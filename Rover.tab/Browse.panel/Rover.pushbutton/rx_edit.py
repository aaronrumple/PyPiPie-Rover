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
        if len(self.elements) == 1 and isinstance(self.elements[0], RENAMABLE):
            el = self.elements[0]
            slot = Slot("P:name", "P")
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
                tip = "Element name"
            else:
                tip = "Instance parameter \u00b7 {} element{}".format(n, "" if n == 1 else "s")
            if slot.editor == "ro":
                tip += " \u00b7 read-only"
            elif len(distinct) > 1:
                tip += " \u00b7 values differ"
            r["Tip"] = "{}\n{}".format(slot.name, tip)
            t.Rows.Add(r)
        t.DefaultView.Sort = "Order ASC, Group ASC, Name ASC"
        return t


# --------------------------------------------------------------------------- choices for 'id' editors

def _label(el):
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


def choices(doc, slot):
    """[(label, value)] the slot can be set to; value is an ElementId (or workset int)."""
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

    ref = None
    for e in els:
        p = slot.find(e)
        if p is not None and p.HasValue:
            v = p.AsElementId()
            if rx_data.is_valid(v) and rx_data.eid_int(v) > 0:
                ref = doc.GetElement(v)
                if ref is not None:
                    break
    candidates = []
    if ref is not None:
        cat_id = ref.Category.Id if ref.Category is not None else None
        try:
            col = DB.FilteredElementCollector(doc).OfClass(ref.GetType())
            candidates = [c for c in col if cat_id is None or (c.Category is not None and c.Category.Id == cat_id)]
        except Exception:
            if cat_id is not None:
                candidates = list(DB.FilteredElementCollector(doc).OfCategoryId(cat_id))
    else:
        low = slot.name.lower()
        if "level" in low:
            candidates = list(DB.FilteredElementCollector(doc).OfClass(DB.Level))
        elif "phase" in low:
            candidates = list(doc.Phases)
        elif "material" in low:
            candidates = list(DB.FilteredElementCollector(doc).OfClass(DB.Material))
    out = sorted([(_label(c), c.Id) for c in candidates], key=lambda kv: kv[0].lower())
    return [("<None>", DB.ElementId.InvalidElementId)] + _dedupe(out)


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


def apply(doc, slot, value):
    """Write value to every target in one transaction. Returns (count written, [error messages])."""
    t = DB.Transaction(doc, "Rover: Set {}".format(slot.name))
    t.Start()
    ok, errors = 0, []
    try:
        for eid in slot.targets:
            el = doc.GetElement(eid)
            if el is None:
                continue
            tag = "{} [{}]".format(rx_data.elem_name(el) or el.GetType().Name, rx_data.eid_int(eid))
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
