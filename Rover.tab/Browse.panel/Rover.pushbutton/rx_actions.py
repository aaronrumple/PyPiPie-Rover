# -*- coding: utf-8 -*-
"""Rover - everything that touches the Revit UI or model.

A modeless window must not call the Revit API directly for selection, view
changes or transactions, so every action is queued here and run inside an
ExternalEvent (valid API context).
"""
from __future__ import unicode_literals
from Autodesk.Revit import DB
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent

import rx_data
import rx_edit
import rx_views

VT = DB.ViewType
MODEL_VIEW_TYPES = rx_views.MODEL_VIEW_TYPES
OPENABLE_VIEW_TYPES = MODEL_VIEW_TYPES | set([VT.DraftingView, VT.Legend, VT.DrawingSheet, VT.Schedule,
                                              VT.ColumnSchedule, VT.PanelSchedule, VT.Report])


class ActionQueue(IExternalEventHandler):
    """Queue of callables fn(uiapp, *args) executed in API context."""

    def __init__(self, report):
        self._queue = []
        self._report = report
        self.event = ExternalEvent.Create(self)   # must be created in API context

    def post(self, fn, *args):
        self._queue.append((fn, args))
        self.event.Raise()

    def Execute(self, uiapp):
        queue, self._queue = self._queue, []
        for fn, args in queue:
            try:
                fn(uiapp, *args)
            except Exception as ex:
                self._report("\u26a0 " + rx_data.err_text(ex))

    def GetName(self):
        return "Rover"


# --------------------------------------------------------------------------- simple actions

def select(uiapp, ids, report=None):
    uiapp.ActiveUIDocument.Selection.SetElementIds(rx_data.id_list(ids))
    if report:
        report("Selected {:,} element(s) in Revit.".format(len(ids)))


def add_to_selection(uiapp, ids, report=None):
    sel = uiapp.ActiveUIDocument.Selection
    current = list(sel.GetElementIds())
    seen = set(rx_data.eid_int(i) for i in current)
    for i in ids:
        if rx_data.eid_int(i) not in seen:
            seen.add(rx_data.eid_int(i))
            current.append(i)
    sel.SetElementIds(rx_data.id_list(current))
    if report:
        report("Revit selection now has {:,} element(s).".format(len(current)))


def reset_isolate(uiapp, report=None):
    """Revit's Reset Temporary Hide/Isolate for the active view."""
    uidoc = uiapp.ActiveUIDocument
    doc, view = uidoc.Document, uidoc.ActiveView
    t = DB.Transaction(doc, "Rover: Reset temporary hide/isolate")
    t.Start()
    try:
        view.DisableTemporaryViewMode(DB.TemporaryViewMode.TemporaryHideIsolate)
        t.Commit()
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise
    if report:
        report("Showing everything in {} again.".format(view.Name))


def zoom(uiapp, ids, report=None):
    uidoc = uiapp.ActiveUIDocument
    lst = rx_data.id_list(ids)
    uidoc.Selection.SetElementIds(lst)
    uidoc.ShowElements(lst)


def isolate(uiapp, ids, report=None):
    uidoc = uiapp.ActiveUIDocument
    doc, view = uidoc.Document, uidoc.ActiveView
    t = DB.Transaction(doc, "Rover: Isolate")
    t.Start()
    try:
        view.IsolateElementsTemporary(rx_data.id_list(ids))
        t.Commit()
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise
    if report:
        report("Temporarily isolated {:,} element(s) in {}.".format(len(ids), view.Name))


def pick(uiapp, done):
    from Autodesk.Revit.UI.Selection import ObjectType
    from Autodesk.Revit.Exceptions import OperationCanceledException
    try:
        ref = uiapp.ActiveUIDocument.Selection.PickObject(ObjectType.Element, "Rover: pick an element")
    except OperationCanceledException:
        return
    done(ref.ElementId)


# --------------------------------------------------------------------------- edits

def apply_edit(uiapp, doc, slot, value, done, per=None):
    """Write a parameter / Name edit (see rx_edit.apply) and report back to the window.

    per: {element id int: value} for pattern edits."""
    ok, errors = rx_edit.apply(doc, slot, value, per)
    done(slot, ok, errors)


def apply_property(uiapp, obj, name, value, done):
    try:
        rx_edit.apply_property(obj, name, value)
    except Exception as ex:
        done(name, rx_data.err_text(ex))
        return
    done(name, None)


def probe_codes(uiapp, doc, slots):
    """Fill rx_edit.CODE_CACHE for coded integer parameters (rolled-back transaction, no model change)."""
    for slot in slots:
        try:
            rx_edit.probe_codes(doc, slot)
        except Exception:
            pass


def duplicate_type(uiapp, doc, type_id, name, done):
    et = doc.GetElement(type_id)
    t = DB.Transaction(doc, "Rover: Duplicate type")
    t.Start()
    try:
        new = et.Duplicate(name)
        t.Commit()
    except Exception:
        if t.HasStarted() and not t.HasEnded():
            t.RollBack()
        raise
    done(new.Id)


# --------------------------------------------------------------------------- open a view with the element

def _usable(view, types):
    try:
        return view is not None and not view.IsTemplate and view.ViewType in types
    except Exception:
        return False


def find_best_view(doc, uidoc, el):
    """Return (view, zoom) - the view to open for this element, or (None, True)."""
    # A view, sheet or schedule: open it directly.
    if isinstance(el, DB.View):
        return (el, False) if _usable(el, OPENABLE_VIEW_TYPES) else (None, False)
    if isinstance(el, DB.Viewport):
        return doc.GetElement(el.SheetId), True

    # View-specific elements (tags, dims, detail items, text): their owner view.
    if rx_data.is_valid(el.OwnerViewId):
        owner = doc.GetElement(el.OwnerViewId)
        if owner is not None:
            return owner, True

    # Active view, then views on sheets (plan at the element's level first), then
    # unplaced plans on that level, then a 3D view - screened geometrically and
    # confirmed with a collector (see rx_views).
    return rx_views.best_view(doc, el, uidoc.ActiveView), True


def open_in_view(uiapp, eid, prefer_view_id=None, report=None):
    """Open the best view showing the element, select it and zoom to it."""
    uidoc = uiapp.ActiveUIDocument
    doc = uidoc.Document
    el = doc.GetElement(eid)
    if el is None:
        if report:
            report("That element no longer exists - refresh the list.")
        return

    view, do_zoom = None, True
    if prefer_view_id is not None:
        pv = doc.GetElement(prefer_view_id)
        if _usable(pv, OPENABLE_VIEW_TYPES):
            view = pv
            do_zoom = pv.ViewType in MODEL_VIEW_TYPES or pv.ViewType == VT.DrawingSheet
    if view is None:
        view, do_zoom = find_best_view(doc, uidoc, el)

    is_view = isinstance(el, DB.View)
    ids = rx_data.id_list([eid])
    if view is None:
        if is_view:
            if report:
                report("{} can't be opened (template or browser view).".format(rx_data.elem_name(el)))
            return
        # Let Revit try - it will say so if no view can show the element.
        uidoc.Selection.SetElementIds(ids)
        uidoc.ShowElements(ids)
        if report:
            report("No placed view shows this element; Revit chose a view.")
        return

    _activate(uiapp, view, None if is_view else ids, do_zoom)
    if report:
        report("Opened {}.".format(view.Name))


def _activate(uiapp, view, ids, do_zoom):
    uidoc = uiapp.ActiveUIDocument
    if uidoc.ActiveView.Id != view.Id:
        try:
            uidoc.ActiveView = view
        except Exception:
            # Not allowed right now - ask Revit to switch, then finish on the next idle.
            uidoc.RequestViewChange(view)
            _finish_when_idle(uiapp, view.Id, ids, do_zoom)
            return
    _finish(uidoc, ids, do_zoom)


def _finish(uidoc, ids, do_zoom):
    if not ids:
        return
    uidoc.Selection.SetElementIds(ids)
    if do_zoom:
        try:
            uidoc.ShowElements(ids)
        except Exception:
            pass


def _finish_when_idle(uiapp, view_id, ids, do_zoom):
    state = {"tries": 0}

    def on_idle(sender, args):
        state["tries"] += 1
        uidoc = uiapp.ActiveUIDocument
        done = uidoc is not None and uidoc.ActiveView.Id == view_id
        if done or state["tries"] > 20:
            uiapp.Idling -= on_idle
            if done:
                _finish(uidoc, ids, do_zoom)

    uiapp.Idling += on_idle
