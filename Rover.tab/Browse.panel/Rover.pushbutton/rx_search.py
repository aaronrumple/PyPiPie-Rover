# -*- coding: utf-8 -*-
"""Rover - search engine and saved searches.

A query is a plain dict (so it can be saved as JSON):

    {"name": "Fire-rated doors", "match": "all" | "any",
     "scope": "doc" | "view" | "sheet" | "selection" | "list",
     "include_types": False, "case": False,
     "conditions": [{"field": "Category", "param": "", "op": "equals", "value": "Doors"}, ...]}

Reading only - nothing here changes the model.
"""
from __future__ import unicode_literals
import io
import json
import os
import re

from Autodesk.Revit import DB

import rx_data

FIELDS = ["Category", "Family", "Type", "Name", "Level", "Workset", "Element Id", "Class",
          "Parameter", "Any parameter value"]
OPS = ["contains", "does not contain", "equals", "does not equal", "starts with", "ends with",
       "greater than", "less than", "at least", "at most", "is empty", "has value", "matches regex"]
NO_VALUE_OPS = set(["is empty", "has value"])
NUMERIC_OPS = set(["greater than", "less than", "at least", "at most"])
SCOPES = [("doc", "Entire document"), ("view", "Active view"), ("sheet", "Active sheet (all viewports)"),
          ("selection", "Current Revit selection"), ("list", "Current Document list")]

STORE_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "pyRevit", "Rover")
STORE_PATH = os.path.join(STORE_DIR, "searches.json")
OLD_STORE_PATH = os.path.join(os.path.dirname(STORE_DIR), "RevitExplorer", "searches.json")

SEED_SEARCHES = [
    {"name": "Fire-rated doors", "match": "all", "scope": "doc", "include_types": False, "case": False,
     "conditions": [{"field": "Category", "param": "", "op": "equals", "value": "Doors"},
                    {"field": "Parameter", "param": "Fire Rating", "op": "has value", "value": ""}]},
    {"name": "Doors without a Mark", "match": "all", "scope": "doc", "include_types": False, "case": False,
     "conditions": [{"field": "Category", "param": "", "op": "equals", "value": "Doors"},
                    {"field": "Parameter", "param": "Mark", "op": "is empty", "value": ""}]},
    {"name": "Rooms not placed or not enclosed", "match": "all", "scope": "doc", "include_types": False, "case": False,
     "conditions": [{"field": "Category", "param": "", "op": "equals", "value": "Rooms"},
                    {"field": "Parameter", "param": "Area", "op": "at most", "value": "0"}]},
    {"name": "Walls taller than 12 ft", "match": "all", "scope": "doc", "include_types": False, "case": False,
     "conditions": [{"field": "Category", "param": "", "op": "equals", "value": "Walls"},
                    {"field": "Parameter", "param": "Unconnected Height", "op": "greater than", "value": "12'"}]},
]


def blank_query(name="New search"):
    return {"name": name, "match": "all", "scope": "doc", "include_types": False, "case": False,
            "conditions": [{"field": "Category", "param": "", "op": "equals", "value": ""}]}


# --------------------------------------------------------------------------- saved searches

def load_searches():
    if not os.path.exists(STORE_PATH) and os.path.exists(OLD_STORE_PATH):
        try:
            with io.open(OLD_STORE_PATH, encoding="utf-8") as f:
                save_searches(json.load(f).get("searches", []))
        except Exception:
            pass
    if not os.path.exists(STORE_PATH):
        return [dict(q) for q in SEED_SEARCHES]
    try:
        with io.open(STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("searches", []) if isinstance(data, dict) else list(data)
    except Exception:
        return [dict(q) for q in SEED_SEARCHES]


def save_searches(searches, path=None):
    path = path or STORE_PATH
    folder = os.path.dirname(path)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    text = json.dumps({"version": 1, "searches": searches}, indent=2, ensure_ascii=True)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text if isinstance(text, type("")) else text.decode("utf-8"))


def import_searches(path):
    with io.open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("searches", []) if isinstance(data, dict) else data
    return [q for q in items if isinstance(q, dict) and "conditions" in q]


# --------------------------------------------------------------------------- comparisons

def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def str_test(op, text, value, case, rx=None):
    a = text or ""
    if op == "is empty":
        return a.strip() == ""
    if op == "has value":
        return a.strip() != ""
    if op == "matches regex":
        return rx is not None and rx.search(a) is not None
    b = value or ""
    if op in NUMERIC_OPS:
        x, y = _num(a), _num(b)
        if x is None or y is None:
            x, y = (a, b) if case else (a.lower(), b.lower())
        return _numeric(op, x, y)
    if not case:
        a, b = a.lower(), b.lower()
    if op == "contains":
        return b in a
    if op == "does not contain":
        return b not in a
    if op == "equals":
        return a == b
    if op == "does not equal":
        return a != b
    if op == "starts with":
        return a.startswith(b)
    if op == "ends with":
        return a.endswith(b)
    return False


def _numeric(op, x, y, tol=1e-9):
    if op == "greater than":
        return x > y + tol if isinstance(x, float) else x > y
    if op == "less than":
        return x < y - tol if isinstance(x, float) else x < y
    if op == "at least":
        return x >= y - tol if isinstance(x, float) else x >= y
    if op == "at most":
        return x <= y + tol if isinstance(x, float) else x <= y
    if op == "equals":
        return abs(x - y) <= max(tol, abs(y) * 1e-9)
    if op == "does not equal":
        return abs(x - y) > max(tol, abs(y) * 1e-9)
    return False


class ParamValueParser(object):
    """User text -> internal number for a parameter, cached per definition."""

    def __init__(self, doc):
        self.doc = doc
        self.units = doc.GetUnits()
        self.cache = {}

    def parse(self, p, text):
        key = (p.Definition.Name, str(p.StorageType), text)
        if key in self.cache:
            return self.cache[key]
        value = None
        if p.StorageType == DB.StorageType.Double:
            try:
                ok, v = DB.UnitFormatUtils.TryParse(self.units, p.Definition.GetDataType(), text)
                if ok:
                    value = v
            except Exception:
                pass
            if value is None:
                value = _num(text)
        elif p.StorageType == DB.StorageType.Integer:
            t = text.strip().lower()
            value = 1.0 if t in ("yes", "true") else 0.0 if t in ("no", "false") else _num(text)
        self.cache[key] = value
        return value


def param_test(doc, parser, p, op, value, case, rx):
    display = rx_data.param_value(doc, p)
    numeric = p.StorageType in (DB.StorageType.Double, DB.StorageType.Integer)
    if numeric and (op in NUMERIC_OPS or op in ("equals", "does not equal")) and (value or "").strip():
        target = parser.parse(p, value)
        if target is not None:
            if not p.HasValue:
                return False, display
            actual = p.AsDouble() if p.StorageType == DB.StorageType.Double else float(p.AsInteger())
            return _numeric(op, actual, target), display
    return str_test(op, display, value, case, rx), display


def find_param(doc, el, name):
    """Instance parameter by name, else the type's."""
    p = el.LookupParameter(name)
    if p is not None:
        return p
    try:
        tid = el.GetTypeId()
        if rx_data.is_valid(tid):
            te = doc.GetElement(tid)
            if te is not None:
                return te.LookupParameter(name)
    except Exception:
        pass
    return None


def all_params(doc, el):
    for p in el.Parameters:
        yield p
    try:
        tid = el.GetTypeId()
        te = doc.GetElement(tid) if rx_data.is_valid(tid) else None
        if te is not None:
            for p in te.Parameters:
                yield p
    except Exception:
        pass


# --------------------------------------------------------------------------- compiled query

class Query(object):
    def __init__(self, doc, idx, q):
        self.doc = doc
        self.idx = idx
        self.q = q
        self.case = bool(q.get("case"))
        self.match_all = q.get("match", "all") != "any"
        self.parser = ParamValueParser(doc)
        self.tests = []
        for c in q.get("conditions", []):
            field, op = c.get("field", "Category"), c.get("op", "contains")
            value = c.get("value", "") or ""
            if field == "Parameter" and not (c.get("param") or "").strip():
                continue
            if op not in NO_VALUE_OPS and not value.strip() and field != "Parameter":
                continue           # an empty condition row is ignored, not "match nothing"
            rx = None
            if op == "matches regex":
                rx = re.compile(value, 0 if self.case else re.IGNORECASE)
            self.tests.append(self._make(field, (c.get("param") or "").strip(), op, value, rx))

    def _make(self, field, pname, op, value, rx):
        doc, idx, case, parser = self.doc, self.idx, self.case, self.parser

        if field == "Parameter":
            def test(el, cat_name):
                p = find_param(doc, el, pname)
                if p is None:
                    return False, None
                ok, shown = param_test(doc, parser, p, op, value, case, rx)
                return ok, "{} = {}".format(pname, shown)
            return test

        if field == "Any parameter value":
            negate = op in ("does not contain", "does not equal")
            positive = {"does not contain": "contains", "does not equal": "equals"}.get(op, op)
            type_hits = {}      # type id -> note or None; type parameters are shared, check once

            def first_hit(params):
                for p in params:
                    try:
                        shown = rx_data.param_value(doc, p)
                    except Exception:
                        continue
                    if shown and str_test(positive, shown, value, case, rx):
                        return "{} = {}".format(p.Definition.Name, shown)
                return None

            def test(el, cat_name):
                note = first_hit(el.Parameters)
                if note is None:
                    try:
                        tid = el.GetTypeId()
                        key = rx_data.eid_int(tid)
                    except Exception:
                        key = -1
                    if key > 0:
                        if key not in type_hits:
                            te = doc.GetElement(tid)
                            type_hits[key] = first_hit(te.Parameters) if te is not None else None
                        note = type_hits[key]
                if negate:
                    return (note is None), (None if note else "no value {} '{}'".format(positive, value))
                return (note is not None), note
            return test

        def getter(el, cat_name):
            if field == "Category":
                return cat_name
            if field == "Family":
                return idx.type_info(el)[0]
            if field == "Type":
                return idx.type_info(el)[1]
            if field == "Name":
                return rx_data.elem_name(el)
            if field == "Level":
                return idx._level(el)
            if field == "Workset":
                return idx._workset(el)
            if field == "Element Id":
                return str(rx_data.eid_int(el.Id))
            if field == "Class":
                return el.GetType().Name
            return ""

        def test(el, cat_name):
            shown = getter(el, cat_name)
            note = None if field == "Category" else "{}: {}".format(field, shown)
            return str_test(op, shown, value, case, rx), note
        return test

    def category_prefilter(self):
        """Category id when a Category-equals condition must hold (AND mode), else None."""
        if not self.match_all:
            return None
        for c in self.q.get("conditions", []):
            if c.get("field") == "Category" and c.get("op") == "equals" and (c.get("value") or "").strip():
                name = c["value"].strip().lower()
                for cat in self.doc.Settings.Categories:
                    if cat.Name.lower() == name:
                        return cat.Id
        return None

    def evaluate(self, el, cat_name):
        if not self.tests:
            return True, ""
        notes = []
        for test in self.tests:
            try:
                ok, note = test(el, cat_name)
            except Exception:
                ok, note = False, None
            if ok:
                if note:
                    notes.append(note)
                if not self.match_all:
                    return True, "; ".join(notes)
            elif self.match_all:
                return False, ""
        return (True, "; ".join(notes)) if self.match_all else (False, "")


# --------------------------------------------------------------------------- scopes and running

def scope_sources(doc, uidoc, scope, include_types=False, cat_id=None, list_ids=None):
    """[(elements iterable, context, ctx_id)] for the scope. cat_id narrows collectors natively.

    Deliberately not a generator: IronPython 2.7 fails to compile generators that
    mix a nested closure with early returns.
    """
    def narrowed(col):
        return col.OfCategoryId(cat_id) if cat_id is not None else col

    if scope == "selection":
        return [([doc.GetElement(i) for i in uidoc.Selection.GetElementIds()], "", 0)]
    if scope == "list":
        return [([doc.GetElement(i) for i in (list_ids or [])], "", 0)]
    if scope == "view":
        view = uidoc.ActiveView
        return [(narrowed(DB.FilteredElementCollector(doc, view.Id)).WhereElementIsNotElementType(),
                 view.Name, rx_data.eid_int(view.Id))]
    if scope == "sheet":
        sheet = rx_data.sheet_for_view(doc, uidoc.ActiveView)
        if sheet is None:
            return []
        views = [sheet] + [doc.GetElement(doc.GetElement(v).ViewId) for v in sheet.GetAllViewports()]
        out = []
        for view in views:
            if view is not None:
                out.append((narrowed(DB.FilteredElementCollector(doc, view.Id)).WhereElementIsNotElementType(),
                            view.Name, rx_data.eid_int(view.Id)))
        return out
    out = [(narrowed(DB.FilteredElementCollector(doc)).WhereElementIsNotElementType(), "", 0)]
    if include_types:
        out.append((narrowed(DB.FilteredElementCollector(doc)).WhereElementIsElementType(), "", 0))
    return out


def run(doc, uidoc, q, list_ids=None, helpers=False):
    """Run a query. Returns (DataTable, Indexer, scanned count). 'Context' holds 'Matched on'."""
    idx = rx_data.Indexer(doc, helpers)
    query = Query(doc, idx, q)
    table = idx.new_table()
    table.BeginLoadData()
    scanned = 0
    seen = set()
    sources = scope_sources(doc, uidoc, q.get("scope", "doc"), q.get("include_types"),
                            query.category_prefilter(), list_ids)
    for elements, ctx, ctx_id in sources:
        for el in elements:
            if el is None:
                continue
            n = rx_data.eid_int(el.Id)
            if n in seen:
                continue
            seen.add(n)
            cat_name = idx.category_of(el)
            if cat_name is None:
                continue
            scanned += 1
            ok, note = query.evaluate(el, cat_name)
            if ok:
                idx.add(table, el, note or ctx, ctx_id)
    table.EndLoadData()
    return table, idx, scanned


# --------------------------------------------------------------------------- suggestions for the editor

def category_names(doc):
    return sorted(set(c.Name for c in doc.Settings.Categories if c.CategoryType == DB.CategoryType.Model
                      or c.CategoryType == DB.CategoryType.Annotation), key=lambda s: s.lower())


def level_names(doc):
    return [rx_data.elem_name(l) for l in sorted(DB.FilteredElementCollector(doc).OfClass(DB.Level),
                                                 key=lambda l: l.Elevation)]


def workset_names(doc):
    if not doc.IsWorkshared:
        return []
    return sorted(w.Name for w in DB.FilteredWorksetCollector(doc).OfKind(DB.WorksetKind.UserWorkset))


def sample_elements(doc, cat_id=None, limit=400):
    col = DB.FilteredElementCollector(doc)
    if cat_id is not None:
        col = col.OfCategoryId(cat_id)
    out = []
    for el in col.WhereElementIsNotElementType():
        if el.Category is None:
            continue
        out.append(el)
        if len(out) >= limit:
            break
    return out


def parameter_names(doc, elements):
    names = set()
    types_seen = set()
    for el in elements:
        for p in el.Parameters:
            names.add(p.Definition.Name)
        try:
            tid = el.GetTypeId()
            k = rx_data.eid_int(tid)
            if k > 0 and k not in types_seen:
                types_seen.add(k)
                for p in doc.GetElement(tid).Parameters:
                    names.add(p.Definition.Name)
        except Exception:
            pass
    return sorted(names, key=lambda s: s.lower())


def parameter_values(doc, elements, pname, limit=60):
    values = set()
    for el in elements:
        p = find_param(doc, el, pname)
        if p is not None and p.HasValue:
            v = rx_data.param_value(doc, p)
            if v:
                values.add(v)
                if len(values) >= limit:
                    break
    return sorted(values, key=lambda s: s.lower())
