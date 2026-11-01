# -*- coding: utf-8 -*-
"""Rover - data access (reading only; edits live in rx_edit).

Element index (as a DataTable for fast WPF filtering), parameter tables,
reflection over API objects (Revit Lookup style) and element relations.
Nothing in this module changes the model.
"""
from __future__ import unicode_literals
import clr
clr.AddReference("System.Data")

import System
from System import Int64, Int32, String, Boolean
from System.Data import DataTable
from System.Collections import IEnumerable
from System.Collections.Generic import List
from System.Reflection import BindingFlags

from Autodesk.Revit import DB

INVALID = DB.ElementId.InvalidElementId

T_STR = clr.GetClrType(String)
T_I64 = clr.GetClrType(Int64)
T_I32 = clr.GetClrType(Int32)
T_BOOL = clr.GetClrType(Boolean)

try:
    NUMBERS = (int, float, long)  # IronPython 2
except NameError:
    NUMBERS = (int, float)        # CPython 3

LIST_CAP = 500  # rows shown when drilling into a collection


# --------------------------------------------------------------------------- ids / names

def eid_int(eid):
    """ElementId -> int across Revit versions (Value in 2024+, IntegerValue before)."""
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def make_eid(n):
    try:
        return DB.ElementId(Int64(n))
    except Exception:
        return DB.ElementId(int(n))


def is_valid(eid):
    return eid is not None and eid != INVALID


def elem_name(el):
    if el is None:
        return ""
    try:
        return el.Name or ""
    except Exception:
        pass
    try:
        return DB.Element.Name.GetValue(el) or ""
    except Exception:
        return ""


def id_list(ids):
    out = List[DB.ElementId]()
    for i in ids:
        out.Add(i)
    return out


def err_text(ex):
    """First useful line of an exception, unwrapping .NET TargetInvocationException."""
    inner = getattr(ex, "clsException", None) or (ex if isinstance(ex, System.Exception) else None)
    while inner is not None and getattr(inner, "InnerException", None) is not None:
        inner = inner.InnerException
    msg = inner.Message if inner is not None else str(ex)
    return ((msg or "error").splitlines() or ["error"])[0][:240]


def level_id(el):
    try:
        lid = el.LevelId
        if is_valid(lid):
            return lid
    except Exception:
        pass
    for bip in (DB.BuiltInParameter.SCHEDULE_LEVEL_PARAM, DB.BuiltInParameter.FAMILY_LEVEL_PARAM):
        try:
            p = el.get_Parameter(bip)
            if p is not None and is_valid(p.AsElementId()):
                return p.AsElementId()
        except Exception:
            pass
    return None


# --------------------------------------------------------------------------- element index

ELEMENT_COLUMNS = [
    ("Id", T_I64), ("Category", T_STR), ("Family", T_STR), ("Type", T_STR), ("Name", T_STR),
    ("Level", T_STR), ("Mark", T_STR), ("Workset", T_STR), ("Context", T_STR), ("CtxId", T_I64),
    ("Count", T_I32), ("Warning", T_STR), ("Occurrence", T_STR), ("Element", T_STR),
]
WARNING_COLUMNS = ["Warning", "Occurrence", "Element"]
TEXT_COLUMNS = ["Category", "Family", "Type", "Name", "Level", "Mark", "Workset", "Context"]

# Sketch lines, automatic sketch dimensions, rail path lines: thousands of helper rows,
# shown only when "Include sketch & helper elements" is ticked.
HELPER_CATEGORY_IDS = set()
for _bic in ("OST_SketchLines", "OST_WeakDims", "OST_AxisOfRotation", "OST_RailingRailPathLines",
             "OST_RailingRailPathExtensionLines", "OST_StairsSketchPathLines", "OST_StairsSketchRunLines",
             "OST_StairsSketchBoundaryLines", "OST_StairsSketchRiserLines", "OST_StairsSketchLandingCenterLines"):
    if hasattr(DB.BuiltInCategory, _bic):
        HELPER_CATEGORY_IDS.add(int(getattr(DB.BuiltInCategory, _bic)))
HELPER_NAME_PARTS = ("Rail Path", "<Sketch")

# Elements without a Revit category that are still worth browsing, listed under these names.
CLASS_CATEGORIES = {
    "Family": "Families", "FillPatternElement": "Fill Patterns", "LinePatternElement": "Line Patterns",
    "ParameterFilterElement": "View Filters", "SelectionFilterElement": "Selection Filters",
    "SharedParameterElement": "Shared Parameters", "ParameterElement": "Project Parameters",
    "GlobalParameter": "Global Parameters", "AppearanceAssetElement": "Appearance Assets",
    "ViewFamilyType": "View Types", "PhaseFilter": "Phase Filters",
}


# Families tab: every element type. Types the Project Browser lists under Families are
# "Loadable family" / "System family"; everything else (text, dimension and view types,
# fluids, pipe schedules, wire materials, links, groups...) is marked "Internal type".
BROWSER_SYSTEM_TYPES = set([
    "WallType", "FloorType", "RoofType", "CeilingType", "CurtainSystemType", "StairsType", "RailingType",
    "TopRailType", "HandRailType", "DuctType", "FlexDuctType", "PipeType", "FlexPipeType", "CableTrayType",
    "ConduitType", "MechanicalSystemType", "PipingSystemType", "WireType", "FasciaType", "GutterType",
    "SlabEdgeType", "WallFoundationType", "ToposolidType", "BuildingPadType", "FilledRegionType",
    "ModelTextType", "BeamSystemType",
])
BROWSER_ELEMENTTYPE_CATEGORIES = set()
for _bic in ("OST_Cornices", "OST_Reveals", "OST_Ramps"):
    if hasattr(DB.BuiltInCategory, _bic):
        BROWSER_ELEMENTTYPE_CATEGORIES.add(int(getattr(DB.BuiltInCategory, _bic)))


def in_project_browser(et):
    """True for types the Project Browser lists under Families."""
    if isinstance(et, DB.FamilySymbol):
        return True
    cls = et.GetType().Name
    if cls in BROWSER_SYSTEM_TYPES:
        return True
    return cls == "ElementType" and et.Category is not None and eid_int(et.Category.Id) in BROWSER_ELEMENTTYPE_CATEGORIES


def _words(name):
    """'DimensionType' -> 'Dimension Type'."""
    out = []
    for i, ch in enumerate(name):
        if i and ch.isupper() and not name[i - 1].isupper():
            out.append(" ")
        out.append(ch)
    return "".join(out)


def type_mark(et):
    try:
        p = et.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_MARK)
        return (p.AsString() or "") if p is not None else ""
    except Exception:
        return ""


class Indexer(object):
    """Turns collector results into DataTable rows, caching type/level/workset lookups."""

    def __init__(self, doc, helpers=False):
        self.doc = doc
        self.helpers = helpers
        self.eids = {}          # int -> ElementId (avoids re-creating ids later)
        self.type_counts = {}   # type id -> instances indexed
        self._types = {}
        self._levels = {}
        self._worksets = {}
        self._wst = doc.GetWorksetTable() if doc.IsWorkshared else None
        self._cats = {}         # category id -> (name, is_helper)

    @staticmethod
    def new_table():
        t = DataTable("Elements")
        for name, typ in ELEMENT_COLUMNS:
            t.Columns.Add(name, typ)
        return t

    def category_of(self, el):
        """Category name to list the element under, or None to leave it out."""
        cat = el.Category
        if cat is not None:
            key = eid_int(cat.Id)
            hit = self._cats.get(key)
            if hit is None:
                name = cat.Name
                helper = key in HELPER_CATEGORY_IDS or any(part in name for part in HELPER_NAME_PARTS)
                hit = self._cats[key] = (name, helper)
            return hit[0] if (self.helpers or not hit[1]) else None
        if isinstance(el, DB.View):
            return "Views"
        cls = el.GetType().Name
        label = CLASS_CATEGORIES.get(cls)
        if label:
            return label
        return ("(No category) " + cls) if self.helpers else None

    def type_info(self, el, count=False):
        """(family name, type name) for any element."""
        if isinstance(el, DB.ElementType):
            fam = ""
            try:
                fam = el.FamilyName
            except Exception:
                pass
            return (fam or el.GetType().Name, elem_name(el))
        try:
            tid = el.GetTypeId()
        except Exception:
            tid = None
        if is_valid(tid):
            key = eid_int(tid)
            if count:
                self.type_counts[key] = self.type_counts.get(key, 0) + 1
            hit = self._types.get(key)
            if hit is None:
                te = self.doc.GetElement(tid)
                fam = ""
                try:
                    fam = te.FamilyName
                except Exception:
                    pass
                hit = (fam or (te.GetType().Name if te is not None else ""), elem_name(te))
                self._types[key] = hit
            return hit
        return (el.GetType().Name, "(untyped)")

    def _level(self, el):
        lid = level_id(el)
        if lid is None:
            return ""
        key = eid_int(lid)
        if key not in self._levels:
            self._levels[key] = elem_name(self.doc.GetElement(lid))
        return self._levels[key]

    def _workset(self, el):
        if self._wst is None:
            return ""
        try:
            wid = el.WorksetId
            key = wid.IntegerValue
        except Exception:
            return ""
        if key not in self._worksets:
            try:
                self._worksets[key] = self._wst.GetWorkset(wid).Name
            except Exception:
                self._worksets[key] = ""
        return self._worksets[key]

    def _fill_row(self, row, el, cat_name, count=False, family_mode=False):
        fam, typ = self.type_info(el, count)
        row["Category"] = cat_name
        row["Family"] = fam
        row["Type"] = typ
        row["Name"] = elem_name(el)
        if family_mode:
            row["Mark"] = type_mark(el)
            return
        mark = ""
        p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
        if p is not None:
            mark = p.AsString() or ""
        row["Level"] = self._level(el)
        row["Mark"] = mark
        row["Workset"] = self._workset(el)

    def add(self, table, el, context="", ctx_id=0):
        cat_name = self.category_of(el)
        if cat_name is None:
            return
        try:
            n = eid_int(el.Id)
            self.eids[n] = el.Id
            row = table.NewRow()
            row["Id"] = Int64(n)
            self._fill_row(row, el, cat_name, count=True)
            row["Context"] = context
            row["CtxId"] = Int64(ctx_id)
            row["Count"] = Int32(0)
            table.Rows.Add(row)
        except Exception:
            pass  # one bad element should never stop the index

    def update_row(self, row, el, family_mode=False):
        """Re-read an edited element into its existing row."""
        cat_name = self.category_of(el) or row["Category"]
        try:
            self._fill_row(row, el, cat_name, family_mode=family_mode)
        except Exception:
            pass

    def _fill(self, table, collector, context="", ctx_id=0):
        for el in collector:
            self.add(table, el, context, ctx_id)

    # ---- scopes
    def scope_document(self):
        t = self.new_table()
        t.BeginLoadData()
        self._fill(t, DB.FilteredElementCollector(self.doc).WhereElementIsNotElementType())
        t.EndLoadData()
        return t

    def scope_view(self, view):
        t = self.new_table()
        t.BeginLoadData()
        self._fill(t, DB.FilteredElementCollector(self.doc, view.Id).WhereElementIsNotElementType(),
                   view.Name, eid_int(view.Id))
        t.EndLoadData()
        return t

    def scope_sheet(self, sheet):
        t = self.new_table()
        t.BeginLoadData()
        label = "{} (sheet)".format(sheet.SheetNumber)
        self._fill(t, DB.FilteredElementCollector(self.doc, sheet.Id).WhereElementIsNotElementType(),
                   label, eid_int(sheet.Id))
        for vpid in sheet.GetAllViewports():
            vp = self.doc.GetElement(vpid)
            view = self.doc.GetElement(vp.ViewId) if vp is not None else None
            if view is None:
                continue
            try:
                self._fill(t, DB.FilteredElementCollector(self.doc, view.Id).WhereElementIsNotElementType(),
                           view.Name, eid_int(view.Id))
            except Exception:
                pass
        t.EndLoadData()
        return t

    def scope_schedules(self):
        t = self.new_table()
        t.BeginLoadData()
        for sched in schedules(self.doc):
            try:
                self._fill(t, DB.FilteredElementCollector(self.doc, sched.Id).WhereElementIsNotElementType(),
                           sched.Name, eid_int(sched.Id))
            except Exception:
                pass
        t.EndLoadData()
        return t

    def count_types(self):
        """Instances per type id, from one pass over the model."""
        counts = {}
        for el in DB.FilteredElementCollector(self.doc).WhereElementIsNotElementType():
            try:
                k = eid_int(el.GetTypeId())
            except Exception:
                continue
            if k > 0:
                counts[k] = counts.get(k, 0) + 1
        return counts

    def scope_families(self):
        """Every element type: Category > Family > Type, marked loadable / system / internal.
        Types only - instances are never read."""
        t = self.new_table()
        t.BeginLoadData()
        for et in DB.FilteredElementCollector(self.doc).WhereElementIsElementType():
            if et.Category is not None:
                cat_name = et.Category.Name
            else:
                cls = et.GetType().Name
                cat_name = CLASS_CATEGORIES.get(cls) or (_words(cls) + "s")
            try:
                n = eid_int(et.Id)
                self.eids[n] = et.Id
                row = t.NewRow()
                row["Id"] = Int64(n)
                self._fill_row(row, et, cat_name, family_mode=True)
                loadable = isinstance(et, DB.FamilySymbol)
                if loadable:
                    row["Context"] = "Loadable family"
                elif in_project_browser(et):
                    row["Context"] = "System family"
                else:
                    row["Context"] = "Internal type"
                row["CtxId"] = Int64(eid_int(et.Family.Id) if loadable else 0)
                row["Count"] = Int32(0)
                t.Rows.Add(row)
            except Exception:
                pass
        t.EndLoadData()
        return t


def _short(indexer, el):
    return "{} {}".format(indexer.category_of(el) or el.GetType().Name, eid_int(el.Id))


def scope_warnings(indexer):
    """Every Revit warning as rows of the elements it involves.
    Returns (table, warning count, kinds of warning, distinct elements)."""
    doc = indexer.doc
    t = indexer.new_table()
    t.BeginLoadData()
    per_kind = {}
    distinct = set()
    warnings = list(doc.GetWarnings())
    for w in warnings:
        desc = (w.GetDescriptionText() or "(no description)").strip()
        sev = str(w.GetSeverity())
        label = desc if sev == "Warning" else "[{}] {}".format(sev, desc)
        n = per_kind[label] = per_kind.get(label, 0) + 1
        pairs = [(i, False) for i in w.GetFailingElements()] + [(i, True) for i in w.GetAdditionalElements()]
        els = [(doc.GetElement(i), extra) for i, extra in pairs]
        els = [(e, extra) for e, extra in els if e is not None]
        shown = ", ".join(_short(indexer, e) for e, _ in els[:3]) + (" \u2026" if len(els) > 3 else "")
        occurrence = "#{:03d}  {}".format(n, shown)
        for el, extra in els:
            try:
                cat_name = indexer.category_of(el) or el.GetType().Name
                k = eid_int(el.Id)
                indexer.eids[k] = el.Id
                distinct.add(k)
                row = t.NewRow()
                row["Id"] = Int64(k)
                indexer._fill_row(row, el, cat_name)
                row["Context"] = label
                row["CtxId"] = Int64(0)
                row["Count"] = Int32(0)
                row["Warning"] = label
                row["Occurrence"] = occurrence
                row["Element"] = "{} \u00b7 {} [{}]{}".format(cat_name, elem_name(el) or el.GetType().Name, k,
                                                             "  (additional)" if extra else "")
                t.Rows.Add(row)
            except Exception:
                pass
    t.EndLoadData()
    return t, len(warnings), len(per_kind), len(distinct)


def instances_of_types(doc, type_ids):
    """ElementIds of every instance of the given types."""
    want = set(eid_int(t) for t in type_ids)
    if not want:
        return []
    col = DB.FilteredElementCollector(doc).WhereElementIsNotElementType()
    if len(want) <= 40:
        pid = DB.ElementId(DB.BuiltInParameter.ELEM_TYPE_PARAM)
        rules = [DB.ElementParameterFilter(DB.ParameterFilterRuleFactory.CreateEqualsRule(pid, t)) for t in type_ids]
        flt = rules[0] if len(rules) == 1 else DB.LogicalOrFilter(List[DB.ElementFilter](rules))
        return list(col.WherePasses(flt).ToElementIds())
    out = []
    for el in col:
        try:
            if eid_int(el.GetTypeId()) in want:
                out.append(el.Id)
        except Exception:
            pass
    return out


def schedules(doc):
    out = []
    for s in DB.FilteredElementCollector(doc).OfClass(DB.ViewSchedule):
        try:
            if s.IsTemplate or s.IsTitleblockRevisionSchedule:
                continue
        except Exception:
            pass
        out.append(s)
    return sorted(out, key=lambda s: s.Name.lower())


def sheet_for_view(doc, view):
    """The active sheet, or the sheet the active view is placed on."""
    if view is None:
        return None
    if isinstance(view, DB.ViewSheet):
        return view
    for vp in DB.FilteredElementCollector(doc).OfClass(DB.Viewport):
        if vp.ViewId == view.Id:
            return doc.GetElement(vp.SheetId)
    for si in DB.FilteredElementCollector(doc).OfClass(DB.ScheduleSheetInstance):
        if si.ScheduleId == view.Id:
            return doc.GetElement(si.OwnerViewId)
    return None


def placed_views(doc):
    """[(view, sheet)] for every viewport in the document."""
    out = []
    for vp in DB.FilteredElementCollector(doc).OfClass(DB.Viewport):
        view = doc.GetElement(vp.ViewId)
        sheet = doc.GetElement(vp.SheetId)
        if view is not None and sheet is not None:
            out.append((view, sheet))
    return out


def visible_in(doc, view_id, eid):
    """True when the element is collected (i.e. visible) in the given view."""
    try:
        return DB.FilteredElementCollector(doc, view_id).WherePasses(
            DB.ElementIdSetFilter(id_list([eid]))).GetElementCount() > 0
    except Exception:
        return False


# --------------------------------------------------------------------------- RowFilter helpers

def esc_value(s):
    return (s or "").replace("'", "''")


def esc_like(s):
    out = []
    for ch in s:
        if ch in "[]*%":
            out.append("[" + ch + "]")
        elif ch == "'":
            out.append("''")
        else:
            out.append(ch)
    return "".join(out)


def equals_expr(fields, values):
    return " AND ".join("[{}] = '{}'".format(f, esc_value(v)) for f, v in zip(fields, values))


def contains_expr(columns, text, with_id=False):
    like = "'%" + esc_like(text) + "%'"
    parts = ["[{}] LIKE {}".format(c, like) for c in columns]
    if with_id:
        parts.append("CONVERT([Id], 'System.String') LIKE " + like)
    return "(" + " OR ".join(parts) + ")"


# --------------------------------------------------------------------------- parameters

def group_label(defn):
    try:
        return DB.LabelUtils.GetLabelForGroup(defn.GetGroupTypeId()) or "Other"   # 2022+
    except Exception:
        pass
    try:
        return DB.LabelUtils.GetLabelFor(defn.ParameterGroup) or "Other"          # older
    except Exception:
        return "Other"


def param_value(doc, p):
    try:
        if not p.HasValue:
            return ""
        st = p.StorageType
        if st == DB.StorageType.String:
            return p.AsString() or ""
        vs = p.AsValueString()
        if st == DB.StorageType.ElementId:
            if vs:
                return vs
            eid = p.AsElementId()
            el = doc.GetElement(eid) if is_valid(eid) else None
            return elem_name(el) if el is not None else str(eid_int(eid))
        if vs is not None:
            return vs
        if st == DB.StorageType.Integer:
            return str(p.AsInteger())
        if st == DB.StorageType.Double:
            return str(p.AsDouble())
    except Exception as ex:
        return "\u26a0 " + err_text(ex)
    return ""


def params_table(doc, el):
    t = DataTable("Parameters")
    for name, typ in (("Group", T_STR), ("Name", T_STR), ("Value", T_STR), ("Storage", T_STR),
                      ("Kind", T_STR), ("ReadOnly", T_BOOL), ("Shared", T_BOOL)):
        t.Columns.Add(name, typ)

    def add_all(owner, kind):
        for p in owner.Parameters:
            try:
                r = t.NewRow()
                r["Group"] = group_label(p.Definition)
                r["Name"] = p.Definition.Name
                r["Value"] = param_value(doc, p)
                r["Storage"] = str(p.StorageType)
                r["Kind"] = kind
                r["ReadOnly"] = bool(p.IsReadOnly)
                r["Shared"] = bool(p.IsShared)
                t.Rows.Add(r)
            except Exception:
                pass

    add_all(el, "I")
    try:
        tid = el.GetTypeId()
        te = doc.GetElement(tid) if is_valid(tid) else None
        if te is not None:
            add_all(te, "T")
    except Exception:
        pass
    t.DefaultView.Sort = "Group ASC, Name ASC"
    return t


# --------------------------------------------------------------------------- reflection

class Listing(object):
    """A collection shown as an inspectable object."""

    def __init__(self, label, items):
        self.label = label
        self.items = list(items)


class Invoke(object):
    """A parameterless method evaluated on demand (double-click)."""

    def __init__(self, obj, method):
        self.obj = obj
        self.method = method

    def run(self, doc):
        try:
            return describe(doc, self.method.Invoke(self.obj, None))
        except Exception as ex:
            return "\u26a0 " + err_text(ex), None


def type_name(obj):
    try:
        return obj.GetType().Name
    except Exception:
        return type(obj).__name__


def describe(doc, val):
    """(display text, drill target or None) for any value."""
    if val is None:
        return "null", None
    if isinstance(val, bool):
        return str(val), None
    if isinstance(val, NUMBERS):
        return str(val), None
    if isinstance(val, str):
        return val, None
    if isinstance(val, Listing):
        return val.label, val
    if isinstance(val, DB.ElementId):
        n = eid_int(val)
        if n < 0:
            return str(n) + ("  (invalid)" if val == INVALID else "  (built-in)"), None
        el = doc.GetElement(val)
        if el is None:
            return str(n), None
        return "{}  [{}]".format(elem_name(el) or el.GetType().Name, n), el
    if isinstance(val, System.Enum):
        return str(val), None
    if isinstance(val, DB.XYZ):
        return "({:.4f}, {:.4f}, {:.4f})".format(val.X, val.Y, val.Z), None
    if isinstance(val, DB.UV):
        return "({:.4f}, {:.4f})".format(val.U, val.V), None
    if isinstance(val, DB.Element):
        return "{}  [{}]".format(elem_name(val) or val.GetType().Name, eid_int(val.Id)), val
    if isinstance(val, DB.Parameter):
        try:
            return "{} = {}".format(val.Definition.Name, param_value(doc, val)), val
        except Exception:
            return "Parameter", val
    if isinstance(val, DB.Category):
        return val.Name, val
    if isinstance(val, IEnumerable):
        items = []
        more = False
        try:
            for i, it in enumerate(val):
                if i >= 5000:
                    more = True
                    break
                items.append(it)
        except Exception as ex:
            return "\u26a0 " + err_text(ex), None
        n = len(items)
        label = "{}{} item{}".format(n, "+" if more else "", "" if n == 1 else "s")
        return label, (Listing(type_name(val), items) if n else None)
    return type_name(val), val


EXCLUDE_METHODS = set(["GetType", "GetHashCode", "ToString", "Dispose", "GetEnumerator",
                       "GetObjectData", "MemberwiseClone", "Equals", "Clone"])
SAFE_PREFIXES = ("Get", "Is", "Has", "Can", "Find", "Lookup", "As", "Count")


def property_editor(prop_type):
    """Editor kind for a writable API property, or '' when Rover can't edit that type."""
    if prop_type.IsEnum:
        return "enum"
    return {"System.Boolean": "bool", "System.String": "text", "System.Int32": "num",
            "System.Int64": "num", "System.Double": "num"}.get(prop_type.FullName, "")


def member_rows(doc, obj):
    """[(name, value text, type name, drill target, editor)] - properties first, then safe getters."""
    rows = []
    if isinstance(obj, Listing):
        for i, it in enumerate(obj.items[:LIST_CAP]):
            text, target = describe(doc, it)
            rows.append(("[{}]".format(i), text, type_name(it), target, ""))
        if len(obj.items) > LIST_CAP:
            rows.append(("\u2026", "{} more not shown".format(len(obj.items) - LIST_CAP), "", None, ""))
        return rows

    flags = BindingFlags.Public | BindingFlags.Instance
    t = obj.GetType()
    seen = set()
    editable = isinstance(obj, DB.Element)
    for prop in sorted(t.GetProperties(flags), key=lambda p: p.Name):
        if prop.Name in seen or prop.GetIndexParameters().Length:
            continue
        seen.add(prop.Name)
        try:
            text, target = describe(doc, prop.GetValue(obj, None))
        except Exception as ex:
            text, target = "\u26a0 " + err_text(ex), None
        editor = ""
        if editable and prop.CanWrite and prop.GetSetMethod() is not None and not text.startswith("\u26a0"):
            editor = property_editor(prop.PropertyType)
        rows.append((prop.Name, text, prop.PropertyType.Name, target, editor))

    for m in sorted(t.GetMethods(flags), key=lambda m: m.Name):
        if (m.IsSpecialName or m.Name in EXCLUDE_METHODS or m.Name in seen
                or m.GetParameters().Length or m.ReturnType.FullName == "System.Void"
                or m.ContainsGenericParameters or not m.Name.startswith(SAFE_PREFIXES)):
            continue
        seen.add(m.Name)
        rows.append((m.Name + "()", "double-click to evaluate", m.ReturnType.Name, Invoke(obj, m), ""))
    return rows


# --------------------------------------------------------------------------- relations

def relations(doc, el):
    """[(group, label, id int, action)] - action is 'inspect' or 'open' (view id)."""
    rows = []

    def add(group, target, action="inspect", label=None):
        if target is None:
            return
        rows.append((group, label or "{}  \u00b7  {}".format(elem_name(target) or target.GetType().Name,
                                                        target.Category.Name if target.Category else target.GetType().Name),
                     eid_int(target.Id), action))

    for attr in ("Host", "SuperComponent", "Room", "FromRoom", "ToRoom", "Space"):
        try:
            add("Host, rooms & spaces", getattr(el, attr, None), label=None if attr in ("Host", "SuperComponent")
                else "{}: {}".format(attr, elem_name(getattr(el, attr))))
        except Exception:
            pass

    lid = level_id(el)
    if lid is not None:
        add("Level", doc.GetElement(lid))

    if is_valid(el.OwnerViewId):
        v = doc.GetElement(el.OwnerViewId)
        if v is not None:
            add("Owner view", v, action="open")

    try:
        deps = [d for d in el.GetDependentElements(None) if d != el.Id]
        for d in deps[:200]:
            add("Dependent elements", doc.GetElement(d))
    except Exception:
        pass

    if isinstance(el, DB.View):
        sheet = sheet_for_view(doc, el)
        if sheet is not None and sheet.Id != el.Id:
            add("Placed on sheet", sheet, action="open",
                label="{} - {}".format(sheet.SheetNumber, sheet.Name))
        return rows

    import rx_views   # imported here: rx_views imports this module
    for view, sheet in rx_views.likely_placed_views(doc, el):
        rows.append(("Shown in views on sheets", "{} \u00b7 {}".format(sheet.SheetNumber, view.Name),
                     eid_int(view.Id), "open"))
    for sched in rx_views.listing_schedules(doc, el):
        rows.append(("Listed in schedules", sched.Name, eid_int(sched.Id), "open"))
    return rows


def relations_table(rows):
    t = DataTable("Relations")
    for name, typ in (("Group", T_STR), ("Label", T_STR), ("Id", T_I64), ("Action", T_STR)):
        t.Columns.Add(name, typ)
    for g, label, n, action in rows:
        r = t.NewRow()
        r["Group"], r["Label"], r["Id"], r["Action"] = g, label, Int64(n), action
        t.Rows.Add(r)
    return t


# --------------------------------------------------------------------------- settings & database roots

def _collect(doc, cls):
    return list(DB.FilteredElementCollector(doc).OfClass(cls))


def elements_by_class(doc):
    groups = {}
    for el in DB.FilteredElementCollector(doc).WhereElementIsNotElementType():
        groups.setdefault(el.GetType().Name, []).append(el)
    for el in DB.FilteredElementCollector(doc).WhereElementIsElementType():
        groups.setdefault(el.GetType().Name + " (type)", []).append(el)
    items = [Listing("{}  ({:,})".format(k, len(v)), v) for k, v in sorted(groups.items())]
    return Listing("Elements by class", items)


def db_roots(uiapp, doc):
    """[(section, label, factory)] for the Settings & Database tree."""
    def worksets():
        if not doc.IsWorkshared:
            return Listing("Worksets (model is not workshared)", [])
        return Listing("Worksets", DB.FilteredWorksetCollector(doc).OfKind(DB.WorksetKind.UserWorkset))

    def schemas():
        from Autodesk.Revit.DB.ExtensibleStorage import Schema
        return Listing("Extensible Storage schemas", Schema.ListSchemas())

    def line_styles():
        lines = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
        return Listing("Line styles", sorted(lines.SubCategories, key=lambda c: c.Name))

    return [
        ("Application", "Application", lambda: uiapp.Application),
        ("Application", "Active UI document", lambda: uiapp.ActiveUIDocument),
        ("Document", "Document", lambda: doc),
        ("Document", "Active view", lambda: uiapp.ActiveUIDocument.ActiveView),
        ("Settings", "Project Information", lambda: doc.ProjectInformation),
        ("Settings", "Units", lambda: doc.GetUnits()),
        ("Settings", "Phases", lambda: Listing("Phases", doc.Phases)),
        ("Settings", "Worksets", worksets),
        ("Settings", "Design Options", lambda: Listing("Design options", _collect(doc, DB.DesignOption))),
        ("Settings", "Object Styles (categories)", lambda: Listing("Categories", sorted(doc.Settings.Categories, key=lambda c: c.Name))),
        ("Settings", "Line Styles", line_styles),
        ("Settings", "Fill Patterns", lambda: Listing("Fill patterns", _collect(doc, DB.FillPatternElement))),
        ("Settings", "Materials", lambda: Listing("Materials", _collect(doc, DB.Material))),
        ("Settings", "Project Location", lambda: doc.ActiveProjectLocation),
        ("Database", "Elements by class", lambda: elements_by_class(doc)),
        ("Database", "Shared parameters", lambda: Listing("Shared parameters", _collect(doc, DB.SharedParameterElement))),
        ("Database", "Extensible Storage schemas", schemas),
        ("Database", "Registered updaters", lambda: Listing("Updaters", DB.UpdaterRegistry.GetRegisteredUpdaterInfos(doc))),
        ("Database", "Linked models", lambda: Listing("Revit links", _collect(doc, DB.RevitLinkInstance))),
        ("Database", "CAD imports & links", lambda: Listing("CAD", _collect(doc, DB.ImportInstance))),
        ("Database", "Warnings", lambda: Listing("Warnings", doc.GetWarnings())),
    ]
