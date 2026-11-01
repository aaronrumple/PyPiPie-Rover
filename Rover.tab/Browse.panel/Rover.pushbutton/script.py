# -*- coding: utf-8 -*-
"""Rover: browse and edit the document, families, settings and database."""
__title__ = "Rover"
__author__ = "Aaron"
__persistentengine__ = True   # modeless window + ExternalEvent need the engine kept alive

import rx_window

rx_window.show(__revit__)  # noqa: F821  (__revit__ is the UIApplication pyRevit provides)
