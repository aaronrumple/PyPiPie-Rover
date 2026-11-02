# -*- coding: utf-8 -*-
"""Rover - 600 x 300 splash shown while the first index is built.

Put an image named splash.png next to this file to use it as the splash; the splash takes
the image's pixel size (capped at 1200 wide), so it is never distorted.
Without one, a themed placeholder is drawn (icon, name and a status line)."""
from __future__ import unicode_literals
import os
import time

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
from System import Uri, Action, TimeSpan, EventHandler
from System.Windows import (Window, WindowStyle, ResizeMode, WindowStartupLocation, Thickness,
                            HorizontalAlignment, VerticalAlignment, FontWeights, FontStyles)
from System.Windows.Controls import Grid, Border, Image, TextBlock, StackPanel, Orientation
from System.Windows.Media import SolidColorBrush, ColorConverter, Stretch, FontFamily
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption, BitmapCreateOptions
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Threading import DispatcherPriority, DispatcherTimer

import rx_theme

HERE = os.path.dirname(os.path.abspath(__file__))
WIDTH, HEIGHT = 600, 300
MIN_SECONDS = 1.0       # keep it up at least this long so it doesn't just flash

PALETTE = {
    "Dark": {"bg": "#22262B", "border": "#434A53", "text": "#E5E8EB", "muted": "#A9B2BC", "accent": "#4EA1F3"},
    "Light": {"bg": "#ECEEF1", "border": "#C6CCD4", "text": "#1C2025", "muted": "#4E5763", "accent": "#0B63C4"},
}


def _brush(hexcolor):
    return SolidColorBrush(ColorConverter.ConvertFromString(hexcolor))


def _bitmap(path):
    bmp = BitmapImage()
    bmp.BeginInit()
    bmp.UriSource = Uri(path)
    bmp.CacheOption = BitmapCacheOption.OnLoad              # don't lock the file
    bmp.CreateOptions = BitmapCreateOptions.IgnoreImageCache  # pick up a replaced image
    bmp.EndInit()
    return bmp


class Splash(object):

    def __init__(self, uiapp=None, status="Loading the document\u2026"):
        self.started = time.time()
        self.win = w = Window()
        w.Width, w.Height = WIDTH, HEIGHT
        w.WindowStyle = getattr(WindowStyle, "None")   # .None is a keyword in Python
        w.ResizeMode = ResizeMode.NoResize
        w.ShowInTaskbar = False
        w.Title = "Rover"
        self.status = None
        self._target = None

        pal = PALETTE.get(rx_theme.current_theme(), PALETTE["Dark"])
        custom = os.path.join(HERE, "splash.png")
        if os.path.exists(custom):
            try:
                bmp = _bitmap(custom)
                img = Image()
                img.Source = bmp
                img.Stretch = Stretch.Fill
                w.Content = img
                # size the splash to the image (600 wide by default), keeping its proportions
                if bmp.PixelWidth > 0 and bmp.PixelHeight > 0:
                    w.Width = min(bmp.PixelWidth, 1200)
                    w.Height = w.Width * bmp.PixelHeight / float(bmp.PixelWidth)
            except Exception:
                w.Content = self._placeholder(pal, status)
        else:
            w.Content = self._placeholder(pal, status)

        # centre over Revit when we can, otherwise on screen
        w.WindowStartupLocation = WindowStartupLocation.CenterScreen
        if uiapp is not None:
            try:
                WindowInteropHelper(w).Owner = uiapp.MainWindowHandle
                w.WindowStartupLocation = WindowStartupLocation.CenterOwner
            except Exception:
                pass

    def _placeholder(self, pal, status):
        frame = Border()
        frame.Background = _brush(pal["bg"])
        frame.BorderBrush = _brush(pal["border"])
        frame.BorderThickness = Thickness(1)

        grid = Grid()
        frame.Child = grid

        row = StackPanel()
        row.Orientation = Orientation.Horizontal
        row.HorizontalAlignment = HorizontalAlignment.Left
        row.VerticalAlignment = VerticalAlignment.Center
        row.Margin = Thickness(40, 0, 40, 20)
        grid.Children.Add(row)

        icon_path = os.path.join(HERE, "rover.png")
        if os.path.exists(icon_path):
            try:
                icon = Image()
                icon.Source = _bitmap(icon_path)
                icon.Width = icon.Height = 150
                icon.Margin = Thickness(0, 0, 32, 0)
                row.Children.Add(icon)
            except Exception:
                pass

        words = StackPanel()
        words.VerticalAlignment = VerticalAlignment.Center
        row.Children.Add(words)

        name = TextBlock()
        name.Text = "Rover"
        name.FontFamily = FontFamily("Segoe UI")
        name.FontSize = 54
        name.FontWeight = FontWeights.SemiBold
        name.Foreground = _brush(pal["text"])
        words.Children.Add(name)

        quote = TextBlock()
        quote.Text = "\u201cWhere am I?\u201d  \u201cIn the Village.\u201d"
        quote.FontSize = 15
        quote.FontStyle = FontStyles.Italic
        quote.Foreground = _brush(pal["muted"])
        quote.Margin = Thickness(2, 0, 0, 0)
        words.Children.Add(quote)

        accent = Border()
        accent.Height = 3
        accent.Width = 64
        accent.HorizontalAlignment = HorizontalAlignment.Left
        accent.Background = _brush(pal["accent"])
        accent.Margin = Thickness(2, 14, 0, 0)
        words.Children.Add(accent)

        self.status = TextBlock()
        self.status.Text = status
        self.status.FontSize = 12
        self.status.Foreground = _brush(pal["muted"])
        self.status.HorizontalAlignment = HorizontalAlignment.Left
        self.status.VerticalAlignment = VerticalAlignment.Bottom
        self.status.Margin = Thickness(16, 0, 16, 12)
        grid.Children.Add(self.status)
        return frame

    def _pump(self):
        """Let WPF draw the splash before the UI thread goes back to indexing."""
        try:
            self.win.Dispatcher.Invoke(Action(lambda: None), DispatcherPriority.Background)
        except Exception:
            pass

    def show(self):
        try:
            self.win.Show()
            self._pump()
        except Exception:
            pass
        return self

    def say(self, text):
        if self.status is not None:
            self.status.Text = text
            self._pump()

    def close_after(self, window=None):
        """Close once `window` has rendered (and MIN_SECONDS have passed); now if no window."""
        def close_later(*args):
            remaining = MIN_SECONDS - (time.time() - self.started)
            if remaining <= 0:
                self.close()
                self._activate()
                return
            timer = DispatcherTimer()
            timer.Interval = TimeSpan.FromSeconds(remaining)

            def tick(s, a):
                timer.Stop()
                self.close()
                self._activate()
            timer.Tick += tick
            timer.Start()

        self._target = window
        if window is None:
            close_later()
        else:
            try:
                window.ContentRendered += EventHandler(close_later)
            except Exception:
                self.close()

    def _activate(self):
        try:
            if self._target is not None:
                self._target.Activate()
        except Exception:
            pass

    def close(self):
        try:
            self.win.Close()
        except Exception:
            pass
