# -*- coding: utf-8 -*-
"""Rover - follow Revit's UI theme (Revit 2024+), light before that."""
from __future__ import unicode_literals
import os

from System.IO import File
from System.Windows.Markup import XamlReader

HERE = os.path.dirname(os.path.abspath(__file__))


def current_theme():
    try:
        from Autodesk.Revit.UI import UIThemeManager, UITheme
        return "Dark" if UIThemeManager.CurrentTheme == UITheme.Dark else "Light"
    except Exception:
        return "Light"


def apply(window, name=None):
    """Swap the window's theme dictionary. Every brush is a DynamicResource, so this is live."""
    name = name or current_theme()
    path = os.path.join(HERE, "Themes", name + ".xaml")
    theme = XamlReader.Parse(File.ReadAllText(path))
    merged = window.Resources.MergedDictionaries
    merged.Clear()
    merged.Add(theme)
    return name
