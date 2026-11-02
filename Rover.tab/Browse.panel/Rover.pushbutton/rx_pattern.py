# -*- coding: utf-8 -*-
"""Rover - pattern values for bulk edits.

A text value containing [tokens] is worked out separately for every element, so parameters that
must differ (Name, Sheet Number, View Name, Mark...) can be edited for a whole selection at once.

  [Name]            the element's current name
  [Value]           this parameter's current value on the element
  [Mark], [Level]   any parameter of the element (instance first, then its type), by name
  [Id] [Family] [Type] [Category]
  [#] [##] [###]    a running number, padded to that many digits (1, 01, 001...)
  [#=101] [##=5]    ...starting at 101 / 05
  [Name|upper]      upper, lower, title, trim
  [Name/old/new]    replace text inside the value (several /a/b pairs may follow)
  [[ and ]]         a literal [ or ]

Example: A-[Name]-01  ->  A-<current name>-01;  D[##=1]  ->  D01, D02, D03...
"""
from __future__ import unicode_literals

import re

from Autodesk.Revit import DB

import rx_data

TOKEN = re.compile(r"\[\[|\]\]|\[([^\[\]]+)\]")
NUMBER = re.compile(r"^(#+)(?:\s*=\s*(-?\d+))?$")
CASES = ("upper", "lower", "title", "trim")


def is_pattern(text):
    """True if the text holds at least one [token] (not just escaped [[ ]])."""
    if not text:
        return False
    return any(m.group(1) for m in TOKEN.finditer(text))


def _param_text(doc, p):
    try:
        if p is None or not p.HasValue:
            return ""
        if p.StorageType == DB.StorageType.String:
            return p.AsString() or ""
        if p.StorageType == DB.StorageType.ElementId:
            el = doc.GetElement(p.AsElementId())
            return rx_data.elem_name(el) if el is not None else ""
        return p.AsValueString() or ""
    except Exception:
        return ""


def _lookup(doc, el, name):
    """A parameter's text on the element or its type; None if neither has it."""
    owners = [el]
    try:
        t = doc.GetElement(el.GetTypeId())
        if t is not None:
            owners.append(t)
    except Exception:
        pass
    for o in owners:
        try:
            p = o.LookupParameter(name)
        except Exception:
            p = None
        if p is not None:
            return _param_text(doc, p)
    return None


def _field(doc, el, name, current):
    low = name.strip().lower()
    if low == "name":
        return rx_data.elem_name(el)
    if low == "value":
        return current
    if low == "id":
        return str(rx_data.eid_int(el.Id))
    if low == "category":
        try:
            return el.Category.Name if el.Category is not None else ""
        except Exception:
            return ""
    if low in ("family", "type"):
        t = el if isinstance(el, DB.ElementType) else None
        if t is None:
            try:
                t = doc.GetElement(el.GetTypeId())
            except Exception:
                t = None
        if low == "type":
            return rx_data.elem_name(t) if t is not None else ""
        try:
            return t.FamilyName if t is not None else ""
        except Exception:
            return ""
    return _lookup(doc, el, name.strip())


def _modify(text, mods):
    for m in mods:
        if not m:
            continue
        if m[0] == "/":
            parts = m.split("/")[1:]
            for i in range(0, len(parts) - 1, 2):
                text = text.replace(parts[i], parts[i + 1])
        else:
            k = m.strip().lower()
            if k == "upper":
                text = text.upper()
            elif k == "lower":
                text = text.lower()
            elif k == "title":
                text = text.title()
            elif k == "trim":
                text = text.strip()
            else:
                raise ValueError("unknown modifier |{}".format(m.strip()))
    return text


def _split(token):
    """'Name/a/b|upper' -> ('Name', ['/a/b', 'upper'])."""
    name, mods = token, []
    cut = len(token)
    for ch in "/|":
        i = token.find(ch)
        if 0 <= i < cut:
            cut = i
    name, rest = token[:cut], token[cut:]
    buf = ""
    for ch in rest:
        if ch == "|":
            if buf:
                mods.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        mods.append(buf)
    return name, mods


def expand(doc, pattern, elements, current_of):
    """[(element, new value)] for each element in order. current_of(el) -> this parameter's current text.

    Raises ValueError naming the first token that can't be worked out."""
    out = []
    for n, el in enumerate(elements):
        def sub(m):
            whole = m.group(0)
            if whole == "[[":
                return "["
            if whole == "]]":
                return "]"
            token = m.group(1)
            num = NUMBER.match(token.strip())
            if num:
                width = len(num.group(1))
                start = int(num.group(2)) if num.group(2) is not None else 1
                return str(start + n).zfill(width)
            name, mods = _split(token)
            value = _field(doc, el, name, current_of(el))
            if value is None:
                raise ValueError("[{}]: no parameter named \u201c{}\u201d on {}".format(
                    token, name.strip(), rx_data.elem_name(el) or el.GetType().Name))
            return _modify(value, mods)
        out.append((el, TOKEN.sub(sub, pattern)))
    return out


def duplicates(pairs):
    """Values produced more than once."""
    seen, dup = set(), []
    for _, v in pairs:
        if v in seen and v not in dup:
            dup.append(v)
        seen.add(v)
    return dup
