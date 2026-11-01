# -*- coding: utf-8 -*-
"""Revit Explorer - fast guesses about which views show an element.

A view-scoped FilteredElementCollector is exact but costs about a second per
view on a real model, so views are first screened geometrically (crop box,
plan view range, section box, hidden category / element) and only the best
few candidates are confirmed with a collector.
"""
from __future__ import unicode_literals
from Autodesk.Revit import DB

import rx_data

VT = DB.ViewType
PLAN_TYPES = set([VT.FloorPlan, VT.CeilingPlan, VT.EngineeringPlan, VT.AreaPlan])
SECTION_TYPES = set([VT.Section, VT.Elevation, VT.Detail])
MODEL_VIEW_TYPES = PLAN_TYPES | SECTION_TYPES | set([VT.ThreeD])


def is_model_view(view):
    try:
        return view is not None and not view.IsTemplate and view.ViewType in MODEL_VIEW_TYPES
    except Exception:
        return False


def element_points(el):
    """The 8 model-space corners of the element's bounding box, or None."""
    try:
        bb = el.get_BoundingBox(None)
    except Exception:
        bb = None
    if bb is None:
        return None
    t, mn, mx = bb.Transform, bb.Min, bb.Max
    return [t.OfPoint(DB.XYZ(x, y, z)) for x in (mn.X, mx.X) for y in (mn.Y, mx.Y) for z in (mn.Z, mx.Z)]


def _local_extents(box, pts):
    inv = box.Transform.Inverse
    local = [inv.OfPoint(p) for p in pts]
    return ([min(p.X for p in local), min(p.Y for p in local), min(p.Z for p in local)],
            [max(p.X for p in local), max(p.Y for p in local), max(p.Z for p in local)])


def _overlaps(box, pts, axes):
    lo, hi = _local_extents(box, pts)
    bmin = [box.Min.X, box.Min.Y, box.Min.Z]
    bmax = [box.Max.X, box.Max.Y, box.Max.Z]
    for ax in axes:
        if hi[ax] < bmin[ax] or lo[ax] > bmax[ax]:
            return False
    return True


def _plane_z(doc, vr, plane, fallback):
    try:
        lid = vr.GetLevelId(plane)
        lv = doc.GetElement(lid) if rx_data.is_valid(lid) and rx_data.eid_int(lid) > 0 else None
        if lv is None:
            return fallback
        return lv.ProjectElevation + vr.GetOffset(plane)
    except Exception:
        return fallback


def plan_z_range(doc, view):
    """(low, high) model Z covered by a plan's view range, generous where unknown."""
    gl = view.GenLevel
    base = gl.ProjectElevation if gl is not None else 0.0
    try:
        vr = view.GetViewRange()
    except Exception:
        return base - 10.0, base + 50.0
    P = DB.PlanViewPlane
    top = _plane_z(doc, vr, P.TopClipPlane, base + 50.0)
    bottom = _plane_z(doc, vr, P.ViewDepthPlane, None)
    if bottom is None:
        bottom = _plane_z(doc, vr, P.BottomClipPlane, base - 10.0)
    return min(bottom, top), max(bottom, top)


def _far_clip_on(view):
    try:
        p = view.get_Parameter(DB.BuiltInParameter.VIEWER_BOUND_FAR_CLIPPING)
        return p is not None and p.AsInteger() != 0
    except Exception:
        return True


def view_may_show(doc, view, el, pts):
    """Cheap test: could this model view show the element? False negatives are rare."""
    try:
        if not is_model_view(view):
            return False
        cat = el.Category
        if cat is not None:
            try:
                if view.GetCategoryHidden(cat.Id):
                    return False
            except Exception:
                pass
        try:
            if el.IsHidden(view):
                return False
        except Exception:
            pass
        vt = view.ViewType
        if vt in PLAN_TYPES:
            lo, hi = plan_z_range(doc, view)
            zs = [p.Z for p in pts]
            if max(zs) < lo or min(zs) > hi:
                return False
            return (not view.CropBoxActive) or _overlaps(view.CropBox, pts, (0, 1))
        if vt in SECTION_TYPES:
            box = view.CropBox
            if _far_clip_on(view):
                axes = (0, 1, 2) if view.CropBoxActive else (2,)
                return _overlaps(box, pts, axes)
            lo, hi = _local_extents(box, pts)
            if lo[2] > box.Max.Z:       # entirely on the viewer's side of the cut plane
                return False
            return (not view.CropBoxActive) or _overlaps(box, pts, (0, 1))
        if vt == VT.ThreeD:
            if view.IsPerspective:
                return False            # cameras: too noisy to count as "shows it"
            if view.IsSectionBoxActive:
                return _overlaps(view.GetSectionBox(), pts, (0, 1, 2))
            return True
    except Exception:
        return True
    return False


def rank(view, lvl_id):
    vt = view.ViewType
    if vt in PLAN_TYPES:
        gl = view.GenLevel
        return 0 if (gl is not None and lvl_id is not None and gl.Id == lvl_id) else 1
    if vt in SECTION_TYPES:
        return 2
    return 3


def likely_placed_views(doc, el):
    """[(view, sheet)] on sheets that probably show the element, best first."""
    pts = element_points(el)
    if pts is None:
        return []
    lvl = rx_data.level_id(el)
    out = [(v, s) for v, s in rx_data.placed_views(doc) if view_may_show(doc, v, el, pts)]
    out.sort(key=lambda vs: (rank(vs[0], lvl), vs[1].SheetNumber))
    return out


def best_view(doc, el, active=None, max_checks=3):
    """A model view that shows the element (confirmed by a collector), or None."""
    pts = element_points(el)
    if pts is None:
        return None
    if active is not None and view_may_show(doc, active, el, pts) and rx_data.visible_in(doc, active.Id, el.Id):
        return active
    checks = 0
    for v, s in likely_placed_views(doc, el):
        if checks >= max_checks:
            break
        checks += 1
        if rx_data.visible_in(doc, v.Id, el.Id):
            return v
    lvl = rx_data.level_id(el)
    plans = [v for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan)
             if is_model_view(v) and v.GenLevel is not None and lvl is not None and v.GenLevel.Id == lvl
             and view_may_show(doc, v, el, pts)]
    for v in plans[:2]:
        if rx_data.visible_in(doc, v.Id, el.Id):
            return v
    for v in DB.FilteredElementCollector(doc).OfClass(DB.View3D):
        if is_model_view(v) and not v.IsSectionBoxActive and view_may_show(doc, v, el, pts):
            if rx_data.visible_in(doc, v.Id, el.Id):
                return v
            break
    return None


def listing_schedules(doc, el):
    """Schedules whose rows include the element (category-screened, then confirmed)."""
    cat = el.Category
    out = []
    for s in rx_data.schedules(doc):
        try:
            cid = s.Definition.CategoryId
        except Exception:
            continue
        if rx_data.is_valid(cid) and (cat is None or cid != cat.Id):
            continue
        if rx_data.visible_in(doc, s.Id, el.Id):
            out.append(s)
    return out
