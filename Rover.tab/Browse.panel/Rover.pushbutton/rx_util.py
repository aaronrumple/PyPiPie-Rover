# -*- coding: utf-8 -*-
"""Rover - small UI helpers shared by the window and the Search panel."""
from __future__ import unicode_literals

from System import TimeSpan
from System.Windows.Threading import DispatcherTimer


class Debounce(object):
    """Call fn once, ms after the last trigger - keeps filter boxes responsive while typing."""

    def __init__(self, ms, fn):
        self.fn = fn
        self.timer = DispatcherTimer()
        self.timer.Interval = TimeSpan.FromMilliseconds(ms)
        self.timer.Tick += self._tick

    def __call__(self, *args):
        self.timer.Stop()
        self.timer.Start()

    def _tick(self, sender, args):
        self.timer.Stop()
        self.fn()
