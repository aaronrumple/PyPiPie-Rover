# -*- coding: utf-8 -*-
"""Rover - editing parameters and properties.

ParamModel turns one or many elements into an editable parameter table:
instance parameters shared by every selected element, type parameters shared
by every distinct type, and - for a single element - its Name. apply() writes
a value to every target inside one transaction, so Ctrl+Z in Revit undoes it.
Read-only parameters are listed but never written.
"""
from __future__ import unicode_literals

import System
from System import Int32, Int64, Double
from System.Data import DataTable

from Autodesk.Revit import DB

import rx_data

VARIES = "<varies>"
BIP = DB.BuiltInParameter


def _bip_ids(*names):
    out = set()
    for n in names:
        if hasattr(BIP, n):
            out.add(int(getattr(BIP, n)))
    return out


TYPE_PARAM_IDS = _bip_ids("ELEM_TYPE_PARAM", "SYMBOL_ID_PARAM", "ELEM_FAMILY_AND_TYPE_PARAM")
WORKSET_PARAM_IDS = _bip_ids("ELEM_PARTITION_PARAM")
READ_ONLY_IDS = _bip_ids("VIEW_FIXED_SKETCH_PLANE")      # writable in the API, meaningless to pick
RENAMABLE = tuple(c for c in (getattr(DB, n, None) for n in (
    "ElementType", "View", "Level", "Grid", "Material", "Family", "FilterElement", "FillPatternElement",
    "LinePatternElement", "Phase", "ReferencePlane")) if c is not None)

COLUMNS = [("Order", rx_data.T_I32), ("Group", rx_data.T_STR), ("Name", rx_data.T_STR), ("Value", rx_data.T_STR),
           ("Storage", rx_data.T_STR), ("Kind", rx_data.T_STR), ("ReadOnly", rx_data.T_BOOL),
           ("Editor", rx_data.T_STR), ("BoolVal", rx_data.T_STR), ("Key", rx_data.T_STR), ("Tip", rx_data.T_STR)]


# --------------------------------------------------------------------------- parameter slots

def is_yes_no(p):
    d = p.Definition
    try:
        return d.GetDataType() == DB.SpecTypeId.Boolean.YesNo
    except Exception:
        pass
    try:
        return d.ParameterType == DB.ParameterType.YesNo
    except Exception:
        return False


def is_coded(p):
    """An Integer parameter Revit shows as a name (Function = Exterior, ...), not a number."""
    try:
        if not p.HasValue:
            return False
        shown = p.AsValueString()
        return bool(shown) and shown.strip() != str(p.AsInteger())
    except Exception:
        return False


def editor_for(p):
    """'ro', 'text', 'num', 'bool', 'code' (named integer) or 'id' (pick an element / workset)."""
    if p.IsReadOnly:
        return "ro"
    st = p.StorageType
    if st == DB.StorageType.String:
        return "text"
    if st == DB.StorageType.Double:
        return "num"
    if st == DB.StorageType.Integer:
        if rx_data.eid_int(p.Id) in WORKSET_PARAM_IDS:
            return "id"
        if is_yes_no(p):
            return "bool"
        return "code" if is_coded(p) else "num"
    if st == DB.StorageType.ElementId:
        if rx_data.eid_int(p.Id) in READ_ONLY_IDS:
            return "ro"
        return "id"
    return "ro"


def slot_key(kind, p):
    """Built-in parameters match by id; others by name + storage so multi-edit works across families."""
    pid = rx_data.eid_int(p.Id)
    if pid < 0:
        return "{}:{}".format(kind, pid)
    return "{}:{}:{}".format(kind, p.Definition.Name, p.StorageType)


class Slot(object):
    """One editable row: a parameter (or Name) and the elements an edit is written to."""

    def __init__(self, key, kind, p=None):
        self.key = key
        self.kind = kind                       # I instance, T type, P property (Name)
        self.pid = rx_data.eid_int(p.Id) if p is not None else 0
        self.name = p.Definition.Name if p is not None else "Name"
        self.group = rx_data.group_label(p.Definition) if p is not None else "Properties"
        self.storage = str(p.StorageType) if p is not None else "String"
        self.editor = editor_for(p) if p is not None else "text"
        self.targets = []
        self.values = []
        self.bools = []
        self.codes = []                        # (label, int) seen on the targets, for 'code' slots

    def find(self, el):
        """This slot's parameter on an element, preferring a writable one."""
        fallback = None
        for p in el.Parameters:
            if self.pid < 0:
                if rx_data.eid_int(p.Id) == self.pid:
                    return p
            elif p.Definition.Name == self.name and str(p.StorageType) == self.storage:
                if not p.IsReadOnly:
                    return p
                fallback = fallback or p
        return fallback


class ParamModel(object):

    def __init__(self, doc, elements):
        self.doc = doc
        self.elements = [e for e in elements if e is not None]
        self.slots = {}
        all_types = bool(self.elements) and all(isinstance(e, DB.ElementType) for e in self.elements)
        self._gather(self.elements, "T" if all_types else "I")
        if not all_types:
            type_ids = {}
            for e in self.elements:
                try:
                    tid = e.GetTypeId()
                except Exception:
                    continue
                if rx_data.is_valid(tid):
                    type_ids[rx_data.eid_int(tid)] = tid
            types = [doc.GetElement(t) for t in type_ids.values()]
            self._gather([t for t in types if t is not None], "T")
        if self.elements and all(isinstance(e, RENAMABLE) for e in self.elements):
            slot = Slot("P:name", "P")
            for el in self.elements:
                slot.targets.append(el.Id)
                slot.values.append(rx_data.elem_name(el))
            self.slots[slot.key] = slot
        self.table = self._build_table()

    def _gather(self, owners, kind):
        if not owners:
            return
        found = {}
        for i, owner in enumerate(owners):
            seen = set()
            for p in owner.Parameters:
                try:
                    key = slot_key(kind, p)
                except Exception:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                slot = found.get(key)
                if slot is None:
                    if i > 0:
                        continue            # not on the first owner, so not shared by all
                    slot = found[key] = Slot(key, kind, p)
                elif p.IsReadOnly:
                    slot.editor = "ro"
                slot.targets.append(owner.Id)
                slot.values.append(rx_data.param_value(self.doc, p))
                if slot.editor == "code":
                    try:
                        slot.codes.append((p.AsValueString(), p.AsInteger()))
                    except Exception:
                        pass
                if slot.editor == "bool" or is_yes_no(p):
                    try:
                        slot.bools.append(1 if p.AsInteger() else 0)
                    except Exception:
                        pass
        n = len(owners)
        for key, slot in found.items():
            if len(slot.targets) == n:
                self.slots[key] = slot

    def _build_table(self):
        t = DataTable("Parameters")
        for name, typ in COLUMNS:
            t.Columns.Add(name, typ)
        for slot in self.slots.values():
            r = t.NewRow()
            distinct = set(slot.values)
            bools = set(slot.bools)
            n = len(slot.targets)
            r["Order"] = Int32(0 if slot.kind == "P" else 1)
            r["Group"] = slot.group
            r["Name"] = slot.name
            r["Value"] = slot.values[0] if len(distinct) == 1 else VARIES
            r["Storage"] = slot.storage
            r["Kind"] = slot.kind if slot.kind != "P" else "I"
            r["ReadOnly"] = slot.editor == "ro"
            r["Editor"] = slot.editor
            r["BoolVal"] = ("1" if 1 in bools else "0") if len(bools) == 1 else ""
            r["Key"] = slot.key
            if slot.kind == "T":
                tip = "Type parameter \u00b7 changes every instance of {} type{}".format(n, "" if n == 1 else "s")
            elif slot.kind == "P":
                tip = "Element name" if n == 1 else \
                    "Name of {} elements \u00b7 names must differ: use a pattern such as A-[Name]-01 or Door [##]".format(n)
            else:
                tip = "Instance parameter \u00b7 {} element{}".format(n, "" if n == 1 else "s")
            if slot.editor == "ro":
                tip += " \u00b7 read-only"
            elif len(distinct) > 1:
                tip += " \u00b7 values differ"
            if slot.editor == "text" and n > 1:
                tip += "\nPatterns: [Name] [Value] [Mark] [#] [##=1] [Name|upper] [Name/old/new]"
            r["Tip"] = "{}\n{}".format(slot.name, tip)
            t.Rows.Add(r)
        t.DefaultView.Sort = "Order ASC, Group ASC, Name ASC"
        return t


# --------------------------------------------------------------------------- choices for 'id' editors

def _label(el):
    if isinstance(el, DB.GraphicsStyle):
        try:
            return el.GraphicsStyleCategory.Name
        except Exception:
            pass
    if isinstance(el, DB.ElementType):
        fam = ""
        try:
            fam = el.FamilyName
        except Exception:
            pass
        name = rx_data.elem_name(el)
        return "{}: {}".format(fam, name) if fam else name
    return rx_data.elem_name(el) or el.GetType().Name


def _dedupe(pairs):
    counts = {}
    for label, _ in pairs:
        counts[label] = counts.get(label, 0) + 1
    out = []
    for label, value in pairs:
        if counts[label] > 1 and isinstance(value, DB.ElementId):
            label = "{}  [{}]".format(label, rx_data.eid_int(value))
        out.append((label, value))
    return out


# --------------------------------------------------------------------------- choices for 'id' editors
# Rover works out what an element-reference parameter can hold without needing a value already set:
#   1. rules for Revit's own parameters (templates, levels, patterns, arrowheads, profiles, fittings...)
#   2. the parameter's declared type (family/shared parameters: Material, Image, <Category> family type)
#   3. a value set on one of the selected elements (same class, same category / family)
#   4. a value set on any other element of the same kind in the model
#   5. the parameter name (level / phase / material)

NONE_LABEL = "<None>"
BIC = DB.BuiltInCategory


def _bic(name):
    return getattr(BIC, name, None)


def _cls(name):
    """A Revit API class by name, wherever it lives (DB, DB.Plumbing, DB.Electrical...)."""
    for ns in (DB, getattr(DB, "Plumbing", None), getattr(DB, "Mechanical", None),
               getattr(DB, "Electrical", None), getattr(DB, "Analysis", None), getattr(DB, "Architecture", None),
               getattr(DB, "Structure", None)):
        if ns is not None:
            c = getattr(ns, name, None)
            if c is not None:
                return c
    return None


def _bip_name(p):
    try:
        return str(p.Definition.BuiltInParameter)
    except Exception:
        return ""


def _of_class(doc, cls):
    if cls is None:
        return []
    try:
        return list(DB.FilteredElementCollector(doc).OfClass(cls))
    except Exception:
        return []


def _types_of(doc, *bic_names):
    out = []
    for n in bic_names:
        b = _bic(n)
        if b is None:
            continue
        try:
            out.extend(DB.FilteredElementCollector(doc).OfCategory(b).WhereElementIsElementType())
        except Exception:
            pass
    return out


def _instances_of(doc, *bic_names):
    out = []
    for n in bic_names:
        b = _bic(n)
        if b is None:
            continue
        try:
            out.extend(DB.FilteredElementCollector(doc).OfCategory(b).WhereElementIsNotElementType())
        except Exception:
            pass
    return out


def _views_of(doc, el):
    """The View an element stands for: itself, or a Viewport's view."""
    if isinstance(el, DB.View):
        return el
    vp = _cls("Viewport")
    if vp is not None and isinstance(el, vp):
        try:
            return doc.GetElement(el.ViewId)
        except Exception:
            return None
    return None


def _view_type_label(vt):
    """FloorPlan -> Floor Plan."""
    t = str(vt)
    out = ""
    for i, ch in enumerate(t):
        if i and ch.isupper() and not t[i - 1].isupper():
            out += " "
        out += ch
    return out.replace("Three D", "3D")


def _template_pairs(doc, els, for_new=False):
    """[(label, id)]: templates of the selection's view type first, then the rest marked with their type."""
    temps = [v for v in _of_class(doc, DB.View) if v.IsTemplate]
    types = set()
    for e in els:
        v = None
        if for_new:
            try:
                tid = e.Id
                v = next((x for x in _of_class(doc, DB.View) if not x.IsTemplate and x.GetTypeId() == tid), None)
            except Exception:
                v = None
        else:
            v = _views_of(doc, e)
        if v is not None:
            types.add(str(v.ViewType))
    match, other = [], []
    for t in temps:
        if not types or str(t.ViewType) in types:
            match.append((_label(t), t.Id))
        else:
            other.append(("{}  ({})".format(_label(t), _view_type_label(t.ViewType)), t.Id))
    key = lambda kv: kv[0].lower()
    if types == set(["Schedule"]):
        other = []                       # schedules only take schedule templates
    return _dedupe(sorted(match, key=key)) + _dedupe(sorted(other, key=key))


def _line_style_pairs(doc, els):
    """[(label, id)] line styles every selected curve accepts, as Revit's Line Style list shows them."""
    common = None
    for e in els:
        try:
            ids = set(rx_data.eid_int(i) for i in e.GetLineStyleIds())
        except Exception:
            return None
        common = ids if common is None else (common & ids)
    out = []
    for n in common or []:
        g = doc.GetElement(rx_data.make_eid(n))
        if g is not None:
            out.append((_label(g), g.Id))
    return _dedupe(sorted(out, key=lambda kv: kv[0].lower()))


def _line_styles(doc):
    """Projection line styles: the Lines category and its subcategories."""
    try:
        lines_id = DB.Category.GetCategory(doc, BIC.OST_Lines).Id
    except Exception:
        return []
    out = []
    for g in _of_class(doc, DB.GraphicsStyle):
        try:
            if g.GraphicsStyleType != DB.GraphicsStyleType.Projection:
                continue
            c = g.GraphicsStyleCategory
            if c is not None and (c.Id == lines_id or (c.Parent is not None and c.Parent.Id == lines_id)):
                out.append(g)
        except Exception:
            pass
    return out


def _arrowheads(doc):
    out = []
    for t in _of_class(doc, DB.ElementType):
        try:
            if t.get_Parameter(BIP.ARROW_TYPE) is not None:
                out.append(t)
        except Exception:
            pass
    return out


def _fill_patterns(doc, drafting_only=False):
    out = []
    for f in _of_class(doc, DB.FillPatternElement):
        try:
            if drafting_only and f.GetFillPattern().Target != DB.FillPatternTarget.Drafting:
                continue
        except Exception:
            pass
        out.append(f)
    return out


def _subcategories(doc, bic_name):
    """(label, id) pairs - subcategories are not elements."""
    try:
        cat = DB.Category.GetCategory(doc, _bic(bic_name))
        return [(c.Name, c.Id) for c in cat.SubCategories]
    except Exception:
        return []


def _fitting_cats(els):
    """Which fitting category a curve type's default fittings come from."""
    names = set()
    for e in els:
        n = e.GetType().Name
        if "Conduit" in n:
            names.add("OST_ConduitFitting")
        elif "CableTray" in n:
            names.add("OST_CableTrayFitting")
        elif "Duct" in n:
            names.add("OST_DuctFitting")
        elif "Pipe" in n:
            names.add("OST_PipeFitting")
    return sorted(names) or ["OST_PipeFitting", "OST_DuctFitting", "OST_ConduitFitting", "OST_CableTrayFitting"]


LEVEL_BIPS = set("""WALL_BASE_CONSTRAINT WALL_HEIGHT_TYPE DPART_BASE_LEVEL FAMILY_BASE_LEVEL_PARAM
FAMILY_TOP_LEVEL_PARAM ROOF_BASE_LEVEL_PARAM ROOF_UPTO_LEVEL_PARAM SCHEDULE_BASE_LEVEL_PARAM
SCHEDULE_TOP_LEVEL_PARAM STAIRS_BASE_LEVEL_PARAM STAIRS_TOP_LEVEL_PARAM STAIRS_RAILING_BASE_LEVEL_PARAM
STAIRS_MULTISTORY_TOP_LEVEL_PARAM FAMILY_LEVEL_PARAM LEVEL_PARAM WALL_SWEEP_LEVEL_PARAM
RBS_ENERGY_ANALYSIS_GROUND_PLANE_PARAM VIEW_UNDERLAY_BOTTOM_ID VIEW_UNDERLAY_TOP_ID GROUP_LEVEL
INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM LEVEL_UP_TO_LEVEL ROOM_UPPER_LEVEL ROOM_LEVEL_ID
INSTANCE_REFERENCE_LEVEL_PARAM STRUCTURAL_BASE_LEVEL STRUCTURAL_TOP_LEVEL""".split())

PHASE_BIPS = set("PHASE_CREATED PHASE_DEMOLISHED VIEW_PHASE RBS_ENERGY_ANALYSIS_PROJECT_PHASE_PARAM".split())

LINE_PATTERN_BIPS = set("""LINE_PATTERN DIM_STYLE_CENTERLINE_PATTERN GRID_CENTER_SEGMENT_PATTERN
GRID_END_SEGMENT_PATTERN""".split())

ARROW_BIPS = set("""LEADER_ARROWHEAD SPOT_ELEV_LEADER_ARROWHEAD DIM_LEADER_ARROWHEAD DIM_STYLE_LEADER_TICK_MARK
DIM_STYLE_INTERIOR_TICK_MARK DIM_STYLE_CENTERLINE_TICK_MARK WITNS_LINE_TICK_MARK ARROWHEAD_TYPE""".split())

PROFILE_BIPS = set("""MULLION_PROFILE REVEAL_PROFILE_PARAM WALL_SWEEP_PROFILE_PARAM SLAB_EDGE_PROFILE_PARAM
FASCIA_PROFILE_PARAM GUTTER_PROFILE_PARAM CONTINUOUSRAIL_PROFILE_TYPE_PARAM STAIRS_TRISERTYPE_RISER_PROFILE
STAIRS_TRISERTYPE_TREAD_PROFILE STAIRS_TRISERTYPE_NOSING_PROFILE STAIRS_ATTR_TREAD_FRONT_PROFILE
STAIRS_SUPPORTTYPE_SECTION_PROFILE""".split())

MATERIAL_BIPS = set("""MATERIAL_ID_PARAM STRUCTURAL_MATERIAL_PARAM RBS_PIPE_MATERIAL_PARAM POCHE_MAT_ID
CONTINUOUSRAIL_MATERIALS_PARAM STAIRS_SUPPORTTYPE_MATERIAL STAIRS_ATTR_BODY_MATERIAL STAIRS_ATTR_RISER_MATERIAL
STAIRS_ATTR_TREAD_MATERIAL STAIRS_ATTR_STRINGER_MATERIAL STAIRS_LANDINGTYPE_LANDING_MATERIAL
STAIRS_RUNTYPE_RUN_MATERIAL STAIRS_TRISERTYPE_RISER_MATERIAL STAIRS_TRISERTYPE_TREAD_MATERIAL RAMP_ATTR_MATERIAL
CONCEPTUAL_CONSTRUCTION_MATERIAL""".split())

MULLION_BIPS = set("""AUTO_MULLION_BORDER1_GRID1 AUTO_MULLION_BORDER1_GRID2 AUTO_MULLION_BORDER1_HORIZ
AUTO_MULLION_BORDER1_VERT AUTO_MULLION_BORDER2_GRID1 AUTO_MULLION_BORDER2_GRID2 AUTO_MULLION_BORDER2_HORIZ
AUTO_MULLION_BORDER2_VERT AUTO_MULLION_INTERIOR_GRID1 AUTO_MULLION_INTERIOR_GRID2 AUTO_MULLION_INTERIOR_HORIZ
AUTO_MULLION_INTERIOR_VERT""".split())

FITTING_BIPS = set("""RBS_CURVETYPE_DEFAULT_BEND_PARAM RBS_CURVETYPE_DEFAULT_CROSS_PARAM RBS_CURVETYPE_DEFAULT_TEE_PARAM
RBS_CURVETYPE_DEFAULT_TAKEOFF_PARAM RBS_CURVETYPE_DEFAULT_TRANSITION_PARAM RBS_CURVETYPE_DEFAULT_UNION_PARAM
RBS_CURVETYPE_DEFAULT_HORIZONTAL_BEND_PARAM RBS_CURVETYPE_DEFAULT_ELBOWUP_PARAM RBS_CURVETYPE_DEFAULT_ELBOWDOWN_PARAM
RBS_CURVETYPE_MULTISHAPE_TRANSITION_PARAM RBS_CURVETYPE_MULTISHAPE_TRANSITION_OVALROUND_PARAM
RBS_CURVETYPE_MULTISHAPE_TRANSITION_RECTOVAL_PARAM""".split())

IMAGE_BIPS = set("ALL_MODEL_IMAGE ALL_MODEL_TYPE_IMAGE".split())


def _by_rule(doc, bip, els):
    """Candidates for a Revit built-in parameter, or None when there is no rule for it."""
    if bip in LEVEL_BIPS:
        return _of_class(doc, DB.Level)
    if bip in PHASE_BIPS:
        return list(doc.Phases)
    if bip == "VIEW_PHASE_FILTER":
        return _of_class(doc, _cls("PhaseFilter"))
    if bip in MATERIAL_BIPS:
        return _of_class(doc, DB.Material)
    if bip in LINE_PATTERN_BIPS:
        return _of_class(doc, DB.LinePatternElement)
    if bip == "COARSE_SCALE_FILL_PATTERN_ID_PARAM":
        return _fill_patterns(doc, drafting_only=True)
    if bip in ("FOREGROUND_ANY_PATTERN_ID_PARAM", "BACKGROUND_DRAFT_PATTERN_ID_PARAM",
               "FOREGROUND_DRAFT_PATTERN_ID_PARAM", "BACKGROUND_ANY_PATTERN_ID_PARAM"):
        return _fill_patterns(doc)
    if bip in ("BUILDING_CURVE_GSTYLE", "BUILDING_CURVE_GSTYLE_PLUS_INVISIBLE"):
        return _line_styles(doc)
    if bip in ARROW_BIPS:
        return _arrowheads(doc)
    if bip in PROFILE_BIPS:
        return _types_of(doc, "OST_ProfileFamilies")
    if bip in MULLION_BIPS:
        return _types_of(doc, "OST_CurtainWallMullions")
    if bip in ("AUTO_PANEL", "AUTO_PANEL_WALL"):
        return _types_of(doc, "OST_CurtainWallPanels")
    if bip in ("CONTINUOUSRAIL_BEGINNING_TERMINATION_TYPE_PARAM", "CONTINUOUSRAIL_END_TERMINATION_TYPE_PARAM"):
        return _types_of(doc, "OST_RailingTermination")
    if bip == "HANDRAIL_SUPPORTS_TYPE_PARAM":
        return _types_of(doc, "OST_RailingSupport")
    if bip in FITTING_BIPS:
        return _types_of(doc, *_fitting_cats(els))
    if bip in IMAGE_BIPS:
        return _of_class(doc, _cls("ImageType"))
    if bip in ("VIEWER_VOLUME_OF_INTEREST_CROP", "DATUM_VOLUME_OF_INTEREST"):
        return _instances_of(doc, "OST_VolumeOfInterest")
    if bip == "SHEET_GUIDE_GRID":
        return _instances_of(doc, "OST_GuideGrid")
    if bip == "VIEW_ANALYSIS_DISPLAY_STYLE":
        return _of_class(doc, _cls("AnalysisDisplayStyle"))
    if bip == "VIEWER_OPTION_VISIBILITY":
        return _of_class(doc, _cls("DesignOption"))
    if bip == "DIM_LABEL":
        return _of_class(doc, _cls("GlobalParameter"))
    if bip == "DIM_STYLE_CENTERLINE_SYMBOL":
        return _types_of(doc, "OST_GenericAnnotation")
    if bip in ("SECTION_ATTR_HEAD_TAG", "SECTION_ATTR_TAIL_TAG"):
        return _types_of(doc, "OST_SectionHeads")
    if bip == "ELEV_SYMBOL_ID":
        return _types_of(doc, "OST_ElevationMarks")
    if bip == "GRID_HEAD_TAG":
        return _types_of(doc, "OST_GridHeads")
    if bip == "LEVEL_HEAD_TAG":
        return _types_of(doc, "OST_LevelHeads")
    if bip == "SPOT_ELEV_SYMBOL":
        return _types_of(doc, "OST_SpotElevSymbols")
    if bip == "VIEWPORT_ATTR_LABEL_TAG":
        return _types_of(doc, "OST_ViewportLabel")
    if bip == "RBS_PIPE_CLASS_PARAM":
        return _of_class(doc, _cls("PipeScheduleType"))
    if bip == "RBS_PIPING_SYSTEM_TYPE_PARAM":
        return _of_class(doc, _cls("PipingSystemType"))
    if bip == "RBS_DUCT_SYSTEM_TYPE_PARAM":
        return _of_class(doc, _cls("MechanicalSystemType"))
    if bip == "RBS_PIPE_FLUID_TYPE_PARAM":
        return _types_of(doc, "OST_PipingFluids") or _of_class(doc, _cls("FluidType"))
    if bip == "RBS_DISTRIBUTIONSYS_VLG_PARAM":
        return _of_class(doc, _cls("VoltageType"))
    if bip == "REVISION_CLOUD_REVISION":
        return _of_class(doc, _cls("Revision"))
    if bip == "STAIRS_TEXT_TYPE":
        return _of_class(doc, _cls("TextNoteType"))
    return None


def _by_datatype(doc, p):
    """Candidates from a family/shared parameter's declared type, or None."""
    try:
        dt = p.Definition.GetDataType()
    except Exception:
        return None
    if dt is None or not dt.TypeId:
        return None
    ref = DB.SpecTypeId.Reference
    try:
        if dt == ref.Material:
            return _of_class(doc, DB.Material)
        if dt == ref.Image:
            return _of_class(doc, _cls("ImageType"))
        if dt == ref.LoadClassification:
            return _of_class(doc, _cls("ElectricalLoadClassification"))
    except Exception:
        pass
    try:
        if DB.Category.IsBuiltInCategory(dt):
            bic = DB.Category.GetBuiltInCategory(dt)
            return list(DB.FilteredElementCollector(doc).OfCategory(bic).WhereElementIsElementType())
    except Exception:
        pass
    return None


def _like(doc, ref):
    """Elements of the same kind as `ref`: same class, same category, same family (types), same template-ness."""
    cat_id = ref.Category.Id if ref.Category is not None else None
    try:
        col = list(DB.FilteredElementCollector(doc).OfClass(ref.GetType()))
    except Exception:
        col = list(DB.FilteredElementCollector(doc).OfCategoryId(cat_id)) if cat_id is not None else [ref]
    out = []
    fam = None
    if isinstance(ref, DB.ElementType) and cat_id is None:
        try:
            fam = ref.FamilyName
        except Exception:
            fam = None
    for c in col:
        if cat_id is not None and (c.Category is None or c.Category.Id != cat_id):
            continue
        if isinstance(ref, DB.View) and c.IsTemplate != ref.IsTemplate:
            continue
        if fam:
            try:
                if c.FamilyName != fam:
                    continue
            except Exception:
                continue
        out.append(c)
    return out


def _sample_ref(doc, slot, els, limit=4000):
    """A value of this parameter set on any element of the same class as the selection."""
    seen = 0
    for cls in set(type(e) for e in els):
        if cls is DB.PropertySetElement:      # asset parameters are not safe to scan
            continue
        try:
            col = DB.FilteredElementCollector(doc).OfClass(cls)
        except Exception:
            continue
        for e in col:
            seen += 1
            if seen > limit:
                return None
            p = slot.find(e)
            if p is None or not p.HasValue:
                continue
            v = p.AsElementId()
            if rx_data.is_valid(v) and rx_data.eid_int(v) > 0:
                r = doc.GetElement(v)
                if r is not None:
                    return r
    return None


def _ref_on(doc, slot, els):
    for e in els:
        p = slot.find(e)
        if p is not None and p.HasValue:
            v = p.AsElementId()
            if rx_data.is_valid(v) and rx_data.eid_int(v) > 0:
                r = doc.GetElement(v)
                if r is not None:
                    return r
    return None


def choices(doc, slot):
    """[(label, value)] the slot can be set to; value is an ElementId (or workset int). Cached on the slot."""
    cached = getattr(slot, "_choices", None)
    if cached is not None:
        return cached
    try:
        out = _choices(doc, slot)
    except Exception:
        out = []                         # never let a drop-down break the inspector
    slot._choices = out
    return out


def _choices(doc, slot):
    els = [doc.GetElement(i) for i in slot.targets]
    els = [e for e in els if e is not None]
    if not els:
        return []
    if slot.pid in WORKSET_PARAM_IDS:
        if not doc.IsWorkshared:
            return []
        ws = DB.FilteredWorksetCollector(doc).OfKind(DB.WorksetKind.UserWorkset)
        return sorted([(w.Name, w.Id.IntegerValue) for w in ws], key=lambda kv: kv[0].lower())
    if slot.pid in TYPE_PARAM_IDS:
        common = None
        for e in els:
            try:
                ids = set(rx_data.eid_int(i) for i in e.GetValidTypes())
            except Exception:
                ids = set()
            common = ids if common is None else (common & ids)
        out = []
        for n in common or []:
            te = doc.GetElement(rx_data.make_eid(n))
            if te is not None:
                out.append((_label(te), te.Id))
        return _dedupe(sorted(out, key=lambda kv: kv[0].lower()))

    p0 = slot.find(els[0])
    bip = _bip_name(p0) if p0 is not None else ""
    pairs = None
    ordered = False                      # pairs already sorted the way they should be shown
    if bip == "CLINE_SUBCATEGORY":
        pairs = _subcategories(doc, "OST_CLines")
    elif bip in ("VIEW_TEMPLATE", "VIEW_TEMPLATE_FOR_SCHEDULE", "DEFAULT_VIEW_TEMPLATE"):
        pairs, ordered = _template_pairs(doc, els, for_new=(bip == "DEFAULT_VIEW_TEMPLATE")), True
    elif bip in ("BUILDING_CURVE_GSTYLE", "BUILDING_CURVE_GSTYLE_PLUS_INVISIBLE"):
        pairs = _line_style_pairs(doc, els)
    candidates = None
    if pairs is None:
        if bip and bip != "INVALID":
            candidates = _by_rule(doc, bip, els)
        if candidates is None and p0 is not None:
            candidates = _by_datatype(doc, p0)
        if candidates is None:
            ref = _ref_on(doc, slot, els) or _sample_ref(doc, slot, els)
            if ref is not None:
                candidates = _like(doc, ref)
        if candidates is None:
            low = slot.name.lower()
            if "level" in low:
                candidates = _of_class(doc, DB.Level)
            elif "phase" in low:
                candidates = list(doc.Phases)
            elif "material" in low:
                candidates = _of_class(doc, DB.Material)
            else:
                candidates = []
        # never offer an element as a value of itself
        own = set(rx_data.eid_int(e.Id) for e in els)
        pairs = [(_label(c), c.Id) for c in candidates if c is not None and rx_data.eid_int(c.Id) not in own]
    if ordered:
        return [(NONE_LABEL, DB.ElementId.InvalidElementId)] + pairs
    out = sorted(pairs, key=lambda kv: kv[0].lower())
    return [(NONE_LABEL, DB.ElementId.InvalidElementId)] + _dedupe(out)


# --------------------------------------------------------------------------- choices for 'code' editors
# Revit has no API that lists a coded integer parameter's names, so Rover asks Revit: inside a
# transaction that is always rolled back it sets each code in turn and reads back the name shown.
# Results are cached per parameter for the session.

CODE_CACHE = {}


def _code_key(slot):
    return slot.key.split(":", 1)[1]


def has_codes(slot):
    return _code_key(slot) in CODE_CACHE


def code_options(slot):
    """[(label, int)] - every name Revit offers once probed, else the values seen on the selection."""
    cached = CODE_CACHE.get(_code_key(slot))
    if cached:
        return cached
    seen, out = set(), []
    for label, value in sorted(slot.codes, key=lambda kv: kv[1]):
        if label and label not in seen:
            seen.add(label)
            out.append((label, value))
    return out


class _Silent(DB.IFailuresPreprocessor):
    """Swallows every warning and error raised while probing, so Revit shows no dialog."""

    def PreprocessFailures(self, accessor):
        try:
            accessor.DeleteAllWarnings()
        except Exception:
            pass
        return DB.FailureProcessingResult.ProceedWithRollBack


def _quiet(t):
    opts = t.GetFailureHandlingOptions()
    opts.SetFailuresPreprocessor(_Silent())
    opts.SetClearAfterRollback(True)
    opts.SetForcedModalHandling(False)
    t.SetFailureHandlingOptions(opts)


def probe_codes(doc, slot, upto=64):
    """Discover every name of a coded integer parameter (model is never changed: always rolled back).
    Tries -1..15, then keeps going until 8 codes in a row produce no name (max upto)."""
    key = _code_key(slot)
    if key in CODE_CACHE:
        return CODE_CACHE[key]
    el = None
    for eid in slot.targets:
        e = doc.GetElement(eid)
        if e is not None and not in_group(e):
            el = e
            break
    if el is None:
        return code_options(slot)
    found, seen = [], set()
    t = DB.Transaction(doc, "Rover: read choices")
    try:
        _quiet(t)
        if t.Start() == DB.TransactionStatus.Started:
            p = slot.find(el)
            if p is not None and not p.IsReadOnly:
                last_hit = -1
                for v in range(-1, upto + 1):
                    if v > 15 and v - last_hit > 8:
                        break
                    try:
                        if not p.Set(v):
                            continue
                        label = p.AsValueString()
                    except Exception:
                        continue
                    if label and label.strip() != str(v):
                        last_hit = v
                        if label not in seen:
                            seen.add(label)
                            found.append((label, v))
    except Exception:
        pass
    finally:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
    if found:
        CODE_CACHE[key] = found
    return code_options(slot)


def option_labels(doc, slot):
    """Labels for a slot's drop-down."""
    if slot.editor == "code":
        return [label for label, _ in code_options(slot)]
    if slot.editor == "id":
        return [label for label, _ in choices(doc, slot)]
    return []


def option_value(doc, slot, label):
    opts = code_options(slot) if slot.editor == "code" else choices(doc, slot)
    for text, value in opts:
        if text == label:
            return value
    return None


def resolve(doc, slot, text):
    """Typed text -> value for an 'id' or 'code' slot: exact label, unique partial match, or a number."""
    t = (text or "").strip()
    low = t.lower()
    if slot.editor == "code":
        opts = code_options(slot)
        for label, value in opts:
            if label.lower() == low:
                return value
        hits = [value for label, value in opts if low and low in label.lower()]
        if len(hits) == 1:
            return hits[0]
        try:
            return int(t)
        except ValueError:
            return None
    opts = choices(doc, slot)
    for label, value in opts:
        if label.lower() == low:
            return value
    hits = [value for label, value in opts if low and low in label.lower()]
    if len(hits) == 1:
        return hits[0]
    try:
        n = int(t)
    except ValueError:
        return None
    return n if slot.pid in WORKSET_PARAM_IDS else rx_data.make_eid(n)


# --------------------------------------------------------------------------- writing

def set_value(doc, p, slot, value):
    st = p.StorageType
    if slot.editor == "bool":
        return bool(p.Set(1 if value else 0))
    if st == DB.StorageType.String:
        return bool(p.Set(value))
    if st == DB.StorageType.ElementId:
        return bool(p.Set(value))
    if st == DB.StorageType.Integer:
        if not isinstance(value, (str, type(""))):
            return bool(p.Set(value))
        try:
            return bool(p.Set(int(value.strip())))
        except ValueError:
            return bool(p.SetValueString(value))
    if st == DB.StorageType.Double:
        try:
            if p.SetValueString(value):
                return True
        except Exception:
            pass
        try:
            ok, v = DB.UnitFormatUtils.TryParse(doc.GetUnits(), p.Definition.GetDataType(), value)
            if ok:
                return bool(p.Set(v))
        except Exception:
            pass
    return False


def set_name(el, value):
    try:
        el.Name = value
    except AttributeError:
        DB.Element.Name.__set__(el, value)      # IronPython hides Name on ElementType


def in_group(el):
    try:
        return rx_data.eid_int(el.GroupId) > 0
    except Exception:
        return False


def apply(doc, slot, value, per=None):
    """Write value to every target in one transaction. Returns (count written, [error messages]).

    per: {element id int: value} - a separate value for each target (pattern edits)."""
    t = DB.Transaction(doc, "Rover: Set {}".format(slot.name))
    t.Start()
    ok, errors = 0, []
    try:
        for eid in slot.targets:
            el = doc.GetElement(eid)
            if el is None:
                continue
            tag = "{} [{}]".format(rx_data.elem_name(el) or el.GetType().Name, rx_data.eid_int(eid))
            if per is not None:
                if rx_data.eid_int(eid) not in per:
                    continue
                value = per[rx_data.eid_int(eid)]
            try:
                if slot.kind != "T" and in_group(el):
                    errors.append("{}: in a group - edit the group to change it".format(tag))
                    continue
                if slot.kind == "P":
                    set_name(el, value)
                    ok += 1
                    continue
                p = slot.find(el)
                if p is None or p.IsReadOnly:
                    errors.append("{}: read-only".format(tag))
                elif set_value(doc, p, slot, value):
                    ok += 1
                else:
                    hint = ""
                    if p.StorageType == DB.StorageType.Integer and p.HasValue:
                        hint = " (enter the number; current {} = {})".format(p.AsInteger(), p.AsValueString())
                    errors.append("{}: Revit did not accept \u201c{}\u201d{}".format(tag, value, hint))
            except Exception as ex:
                errors.append("{}: {}".format(tag, rx_data.err_text(ex)))
        if ok:
            status = t.Commit()
            if status != DB.TransactionStatus.Committed:
                errors.insert(0, "Revit rolled the change back ({})".format(status))
                ok = 0
        else:
            t.RollBack()
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise
    return ok, errors


# --------------------------------------------------------------------------- API properties (Lookup tab)

def find_property(obj, name):
    for prop in obj.GetType().GetProperties():
        if prop.Name == name and prop.CanWrite and prop.GetSetMethod() is not None:
            return prop
    return None


def property_value(prop_type, text):
    """Typed text -> .NET value for a property setter. Raises ValueError on bad input."""
    n = prop_type.FullName
    if prop_type.IsEnum:
        try:
            return System.Enum.Parse(prop_type, text.strip(), True)
        except Exception:
            raise ValueError("not one of " + ", ".join(System.Enum.GetNames(prop_type)))
    if n == "System.Int32":
        return Int32(int(text))
    if n == "System.Int64":
        return Int64(int(text))
    if n == "System.Double":
        return Double(float(text))
    return text


def apply_property(obj, name, value):
    prop = find_property(obj, name)
    if prop is None:
        raise ValueError("{} cannot be set".format(name))
    t = DB.Transaction(obj.Document, "Rover: Set {}".format(name))
    t.Start()
    try:
        prop.SetValue(obj, value, None)
        t.Commit()
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise
