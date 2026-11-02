# -*- coding: utf-8 -*-
"""Rover - modeless WPF window: Document, Families, Search and Settings & Database."""
from __future__ import unicode_literals
import os

import clr
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System.Data")

from System import AppDomain, String, Object, Action
from System.Collections.Generic import List
from System.Data import DataTable
from System.IO import File
from System.Text import Encoding
from System.Windows import (Visibility, Clipboard, FontWeights, Thickness, RoutedEventHandler, Rect,
                            FrameworkElement, RequestBringIntoViewEventHandler)
from System.Windows.Controls import (ItemsControl, TreeViewItem, StackPanel, TextBlock, TextBox, CheckBox, Button, Orientation,
                                     ComboBox, SelectionChangedEventHandler)
from System.Windows.Controls.Primitives import ButtonBase, Selector
from System.Windows.Media import VisualTreeHelper
from System.Windows.Data import CollectionViewSource, PropertyGroupDescription
from System.Windows.Input import Cursors, Mouse, Key
from System.Windows import WindowState
from System.Windows.Threading import DispatcherPriority

from pyrevit import forms
from Autodesk.Revit import DB

import rx_data
import rx_actions
import rx_edit
import rx_theme
import rx_quotes
import rx_search_ui
import rx_pattern
from rx_util import Debounce

HERE = os.path.dirname(os.path.abspath(__file__))
XAML = os.path.join(HERE, "RoverWindow.xaml")
APP_KEY = "Rover.Window"

GROUPINGS = {
    "doc": [
        ("Category \u203a Family \u203a Type", ("Category", "Family", "Type")),
        ("Category \u203a Level", ("Category", "Level")),
        ("Level \u203a Category \u203a Family", ("Level", "Category", "Family")),
        ("Workset \u203a Category", ("Workset", "Category")),
        ("View / Schedule \u203a Category \u203a Type", ("Context", "Category", "Type")),
    ],
    "warn": [
        ("Warning \u203a Occurrence \u203a Element", ("Warning", "Occurrence", "Element")),
        ("Warning \u203a Category", ("Warning", "Category")),
        ("Category \u203a Warning", ("Category", "Warning")),
        ("Level \u203a Warning", ("Level", "Warning")),
    ],
    "fam": [
        ("Category \u203a Family", ("Category", "Family")),
        ("Family kind \u203a Category \u203a Family", ("Context", "Category", "Family")),
    ],
}
CONTEXT_GROUPING = 4
C_NAME, C_LEVEL, C_MARK, C_WORKSET, C_CONTEXT, C_COUNT = 4, 5, 6, 7, 8, 9
DOC_SCOPES = ("doc", "view", "sheet", "sched")
MULTI_CAP = 2000


def show(uiapp):
    """Open Rover, or bring the open window to the front."""
    existing = AppDomain.CurrentDomain.GetData(APP_KEY)
    if existing is not None:
        try:
            if existing.IsLoaded:
                if type(existing).__name__ == "RoverWindow" and type(existing) is not RoverWindow:
                    # opened by code from before a pyRevit reload: replace it with the new version
                    existing.Close()
                else:
                    if existing.WindowState == WindowState.Minimized:
                        existing.WindowState = WindowState.Normal
                    existing.Show()
                    existing.Activate()
                    return existing
        except Exception:
            pass
        AppDomain.CurrentDomain.SetData(APP_KEY, None)
    splash = None
    try:
        import rx_splash
        splash = rx_splash.Splash(uiapp).show()
    except Exception:
        splash = None
    try:
        win = RoverWindow(uiapp)
    except Exception:
        if splash is not None:
            splash.close()
        raise
    AppDomain.CurrentDomain.SetData(APP_KEY, win)
    if splash is not None:
        splash.close_after(win)
    win.show()          # pyRevit modeless
    return win


def row_of(ctrl):
    """The DataRowView behind a cell control, or None."""
    try:
        rv = ctrl.DataContext
        if rv is not None and rv["Key"] is not None:
            return rv
    except Exception:
        pass
    return None


class CellEditing(object):
    """Routes edits from a ListView's value cells (TextBox, CheckBox, drop-down) to callbacks.

    options(row) supplies a drop-down's items the first time it is clicked;
    on_choice(row, label, combo) fires when an item is picked from the open list."""

    def __init__(self, listview, on_text, on_bool, on_pick=None, on_choice=None, options=None):
        self.lv = listview
        self.on_text, self.on_bool, self.on_pick = on_text, on_bool, on_pick
        self.on_choice, self.options = on_choice, options
        self._filling = False
        listview.PreviewKeyDown += self._key
        listview.LostKeyboardFocus += self._lost
        listview.AddHandler(ButtonBase.ClickEvent, RoutedEventHandler(self._click))
        listview.PreviewMouseLeftButtonDown += self._mouse
        listview.AddHandler(Selector.SelectionChangedEvent, SelectionChangedEventHandler(self._selected))

    def _combo_of(self, el):
        while el is not None and el is not self.lv:
            if isinstance(el, ComboBox):
                return el
            try:
                el = VisualTreeHelper.GetParent(el)
            except Exception:
                return None
        return None

    def _mouse(self, sender, args):
        if self.options is None:
            return
        combo = self._combo_of(args.OriginalSource)
        if combo is None or combo.ItemsSource is not None:
            return
        rv = row_of(combo)
        if rv is None:
            return
        items = self.options(rv)
        if items:
            self._filling = True
            try:
                text = combo.Text
                combo.ItemsSource = items
                combo.Text = text
            finally:
                self._filling = False

    def _selected(self, sender, args):
        combo = args.OriginalSource
        if self._filling or self.on_choice is None or not isinstance(combo, ComboBox):
            return
        if args.AddedItems.Count == 0 or not combo.IsDropDownOpen:
            return
        rv = row_of(combo)
        if rv is None:
            return
        label = str(args.AddedItems[0])
        try:
            if label == rv["Value"]:
                return
        except Exception:
            return
        self.on_choice(rv, label, combo)

    def _key(self, sender, args):
        tb = args.OriginalSource
        if not isinstance(tb, TextBox):
            return
        rv = row_of(tb)
        if rv is None:
            return
        if args.Key == Key.Return:
            args.Handled = True
            self.on_text(rv, tb.Text, tb)
        elif args.Key == Key.Escape:
            args.Handled = True
            tb.Text = rv["Value"]

    def _lost(self, sender, args):
        tb = args.OriginalSource
        if not isinstance(tb, TextBox):
            return
        rv = row_of(tb)
        try:
            changed = rv is not None and tb.Text != rv["Value"]
        except Exception:
            changed = False
        if changed:
            self.on_text(rv, tb.Text, tb)

    def _click(self, sender, args):
        src = args.OriginalSource
        rv = row_of(src)
        if rv is None:
            return
        if isinstance(src, CheckBox):
            self.on_bool(rv)
        elif isinstance(src, Button) and self.on_pick is not None:
            self.on_pick(rv)


class MemberView(object):
    """Revit Lookup-style member list; writable properties of elements are editable."""

    def __init__(self, win, listview, on_drill):
        self.win = win
        self.lv = listview
        self.on_drill = on_drill
        self.obj = None
        self.table = None
        self.targets = []
        self._filter = ""
        self.pending = {}
        listview.MouseDoubleClick += self._dbl
        CellEditing(listview, self._on_text, self._on_bool, None, self._on_choice, self._options)

    def fill(self, obj):
        t = DataTable("Members")
        for name, typ in (("Name", rx_data.T_STR), ("Value", rx_data.T_STR), ("Type", rx_data.T_STR),
                          ("Drill", rx_data.T_BOOL), ("Idx", rx_data.T_I32), ("Editor", rx_data.T_STR),
                          ("BoolVal", rx_data.T_STR), ("Key", rx_data.T_STR)):
            t.Columns.Add(name, typ)
        self.obj = obj
        self.targets = []
        self.pending = {}
        if obj is not None:
            for name, text, tname, target, editor in rx_data.member_rows(self.win.doc, obj):
                r = t.NewRow()
                r["Name"], r["Value"], r["Type"] = name, text, tname
                r["Drill"] = target is not None
                r["Idx"] = len(self.targets)
                r["Editor"] = editor or ("link" if target is not None else "")
                r["BoolVal"] = "1" if text == "True" else "0"
                r["Key"] = str(len(self.targets))
                self.targets.append(target)
                t.Rows.Add(r)
        self.table = t
        self.lv.ItemsSource = t.DefaultView
        self.filter(self._filter)

    def filter(self, text):
        self._filter = text or ""
        if self.table is not None:
            self.table.DefaultView.RowFilter = (rx_data.contains_expr(["Name", "Value", "Type"], self._filter)
                                                if self._filter else "")

    def _dbl(self, sender, args):
        if isinstance(args.OriginalSource, TextBox):
            return
        rv = self.lv.SelectedItem
        if rv is None:
            return
        idx = int(rv["Idx"])
        target = self.targets[idx]
        if target is None:
            return
        if isinstance(target, rx_data.Invoke):
            text, result = target.run(self.win.doc)
            rv.Row["Value"] = text
            rv.Row["Drill"] = result is not None
            rv.Row["Editor"] = "link" if result is not None else ""
            self.targets[idx] = result
            return
        self.on_drill(target, rv["Name"])

    # ---- property edits
    def _prop_type(self, name):
        prop = rx_edit.find_property(self.obj, name)
        return prop.PropertyType if prop is not None else None

    def _on_text(self, rv, text, tb):
        try:
            live = Object.ReferenceEquals(rv.Row.Table, self.table)
        except Exception:
            live = False
        if not live or rv["Editor"] not in ("text", "num", "enum") or text == rv["Value"]:
            return
        if self.pending.get(rv["Name"]) == text:
            return
        ptype = self._prop_type(rv["Name"])
        if ptype is None:
            return
        try:
            value = rx_edit.property_value(ptype, text)
        except ValueError as ex:
            self.win.report("{}: {}".format(rv["Name"], ex))
            tb.Text = rv["Value"]
            return
        self._post(rv["Name"], value, text)

    def _on_bool(self, rv):
        if rv["Editor"] == "bool":
            value = rv["BoolVal"] != "1"
            self._post(rv["Name"], value, str(value))

    def _options(self, rv):
        if rv["Editor"] != "enum":
            return None
        ptype = self._prop_type(rv["Name"])
        if ptype is None:
            return None
        import System
        return list(System.Enum.GetNames(ptype))

    def _on_choice(self, rv, label, combo):
        if rv["Editor"] != "enum" or self.pending.get(rv["Name"]) == label:
            return
        ptype = self._prop_type(rv["Name"])
        if ptype is not None:
            self._post(rv["Name"], rx_edit.property_value(ptype, label), label)

    def _post(self, name, value, shown):
        self.pending[name] = shown
        self.win.report("Setting {} = {}\u2026".format(name, shown))
        self.win.actions.post(rx_actions.apply_property, self.obj, name, value, self._done)

    def _done(self, name, error):
        if error:
            self.win.report("{} not changed: {}".format(name, error))
        else:
            self.win.report("{} changed. Undo with Ctrl+Z in Revit.".format(name))
        self.fill(self.obj)
        self.win.after_property_change(self.obj)
        self.win.say("pushed" if error else ("free" if name == "Name" else "hook"))


class RoverWindow(forms.WPFWindow):

    def __init__(self, uiapp):
        forms.WPFWindow.__init__(self, XAML)
        self.uiapp = uiapp
        self.uidoc = uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document
        self.actions = rx_actions.ActionQueue(self.report)

        self.theme = rx_theme.apply(self)
        self.ThemeLabel.Text = "Follows Revit theme: " + self.theme
        self._set_icon()

        self.mode = "doc"
        self.scope = "doc"
        self.indexer = None
        self.table = None
        self.type_counts = None
        self._tree_cache = {}
        self._mode_state = {}       # mode -> (table, indexer, info, scope) so tab switches don't re-index
        self._node_filter = ""
        self._loading = False

        self.history = []
        self.current = None
        self.sel_els = []
        self.param_model = None
        self.pending = {}
        self.rel_table = None
        self._members_for = None
        self._rel_for = None
        self.members = MemberView(self, self.MemberList, lambda target, label: self.inspect(target, label))
        CellEditing(self.ParamList, self.on_param_text, self.on_param_bool, None,
                    self.on_param_choice, self.on_param_options)

        self.db_history = []
        self.db_current = None
        self.db_members = MemberView(self, self.DbMembers, lambda target, label: self.db_show(target, label))

        self._set_groupings()
        self._wire()
        self._bringing = False
        for tree in (self.Tree, self.DbTree):
            tree.AddHandler(FrameworkElement.RequestBringIntoViewEvent,
                            RequestBringIntoViewEventHandler(self._on_tree_bring_into_view))
        self._subscribe()
        self.load_scope("doc")
        self.build_db_tree()
        self.search_ui = rx_search_ui.SearchPanel(self)

    # ------------------------------------------------------------------ plumbing
    def _set_icon(self):
        try:
            from System import Uri
            from System.Windows.Media.Imaging import BitmapImage
            self.Icon = BitmapImage(Uri(os.path.join(HERE, "rover.png")))
            self.TitleIcon.Source = self.Icon
        except Exception:
            pass

    def _no_auto_scroll(self, sender, e):
        """Attached to every tree item: the item's own handler runs before the tree's ScrollViewer,
        so selecting or expanding never scrolls the tree - scrolling is left to the user."""
        e.Handled = True

    def _on_tree_bring_into_view(self, sender, e):
        """Selecting or expanding a node never scrolls the tree - scrolling is left to the user,
        so the expand arrows stay where they are."""
        if isinstance(e.OriginalSource, TreeViewItem):
            e.Handled = True

    def say(self, key, flush=False):
        """Put the line for this task in the title bar; flush paints it before a long step."""
        text = rx_quotes.QUOTES.get(key)
        if not text:
            return
        self.TitleQuote.Text = "\u201c{}\u201d".format(text)
        if flush:
            try:
                self.Dispatcher.Invoke(Action(lambda: None), DispatcherPriority.Render)
            except Exception:
                pass

    # ------------------------------------------------------------------ tree right-click menus
    def _wire_tree_menus(self):
        f = self.FindName
        f("TreeExpandAll").Click += lambda s, a: self._expand_all(self.Tree, True)
        f("TreeCollapseAll").Click += lambda s, a: self._expand_all(self.Tree, False)
        f("TreeIsolate").Click += lambda s, a: self.isolate_node()
        f("TreeShowAll").Click += lambda s, a: self.actions.post(rx_actions.reset_isolate, self.report)
        f("TreeIsolateTree").Click += lambda s, a: self._isolate_branch(self.Tree)
        f("TreeShowAllTree").Click += lambda s, a: self._show_all_branches(self.Tree)
        f("DbIsolateTree").Click += lambda s, a: self._isolate_branch(self.DbTree)
        f("DbShowAllTree").Click += lambda s, a: self._show_all_branches(self.DbTree)
        f("DbExpandAll").Click += lambda s, a: self._expand_all(self.DbTree, True)
        f("DbCollapseAll").Click += lambda s, a: self._expand_all(self.DbTree, False)
        for tree in (self.Tree, self.DbTree):
            tree.PreviewMouseRightButtonDown += self._tree_right_click
            tree.ContextMenuOpening += self._tree_menu_opening

    def _tree_right_click(self, sender, args):
        """Right-click selects the node under the pointer, so the menu acts on it."""
        el = args.OriginalSource
        while el is not None and el is not sender:
            if isinstance(el, TreeViewItem):
                el.IsSelected = True
                return
            try:
                el = VisualTreeHelper.GetParent(el)
            except Exception:
                return

    def _tree_menu_opening(self, sender, args):
        menu = sender.ContextMenu
        if menu is None:
            return
        menu.Resources.MergedDictionaries.Clear()          # popups don't inherit the window's theme brushes
        for d in self.Resources.MergedDictionaries:
            menu.Resources.MergedDictionaries.Add(d)
        has_node = sender.SelectedItem is not None
        self.FindName("TreeIsolateTree" if sender is self.Tree else "DbIsolateTree").IsEnabled = has_node
        if sender is self.Tree:
            on = self.mode != "fam" and self.table is not None
            self.FindName("TreeIsolate").IsEnabled = on and self.Tree.SelectedItem is not None
            self.FindName("TreeShowAll").IsEnabled = self.mode != "fam"

    def _isolate_branch(self, tree):
        """Show only the selected node's branch: its ancestors, itself and everything below it."""
        item = tree.SelectedItem
        if not isinstance(item, TreeViewItem):
            return
        node = item
        while node is not None:
            parent = ItemsControl.ItemsControlFromItemContainer(node)
            if parent is None:
                break
            for sibling in parent.Items:
                if isinstance(sibling, TreeViewItem):
                    sibling.Visibility = Visibility.Visible if sibling is node else Visibility.Collapsed
            if parent is tree:
                break
            parent.IsExpanded = True
            node = parent
        self._set_branch_visible(item, True)
        item.IsExpanded = True

    def _set_branch_visible(self, item, visible):
        stack = [item]
        while stack:
            it = stack.pop()
            if isinstance(it, TreeViewItem):
                it.Visibility = Visibility.Visible if visible else Visibility.Collapsed
                stack.extend(it.Items)

    def _show_all_branches(self, tree):
        for it in tree.Items:
            self._set_branch_visible(it, True)

    def _expand_all(self, tree, expand):
        Mouse.OverrideCursor = Cursors.Wait
        try:
            stack = list(tree.Items)
            while stack:
                item = stack.pop()
                if isinstance(item, TreeViewItem) and item.Items.Count:
                    item.IsExpanded = expand
                    stack.extend(item.Items)
        finally:
            Mouse.OverrideCursor = None

    def isolate_node(self):
        """Temporarily isolate the selected tree node's elements in the active Revit view."""
        item = self.Tree.SelectedItem
        if item is None or item.Tag is None or self.table is None:
            return
        node_filter = item.Tag[0]
        try:
            rows = self.table.Select(node_filter) if node_filter else list(self.table.Rows)
        except Exception as ex:
            self.report("Could not read that node: " + rx_data.err_text(ex))
            return
        ids = self._row_ids(rows)
        if not ids:
            self.report("Nothing to isolate.")
            return
        self.post_ids(rx_actions.isolate, ids)

    def _filter_box(self, ctrl):
        """The filter TextBox a control belongs to (the box itself, or its clear button), else None."""
        if isinstance(ctrl, TextBox) and ctrl.Style is self.FindResource("rx.FilterBox"):
            return ctrl
        if isinstance(ctrl, Button) and ctrl.Name == "PART_Clear" and isinstance(ctrl.TemplatedParent, TextBox):
            return ctrl.TemplatedParent
        return None

    def _on_filter_clear(self, sender, args):
        src = args.OriginalSource
        if isinstance(src, Button):
            box = self._filter_box(src)
            if box is not None:
                box.Text = ""
                box.Focus()
                args.Handled = True

    def _open_link(self, sender, args):
        try:
            from System.Diagnostics import Process
            Process.Start(args.Uri.AbsoluteUri)
        except Exception:
            pass
        args.Handled = True

    def show_help(self):
        path = os.path.join(HERE, "Rover_help.html")
        try:
            os.startfile(path)
        except Exception as ex:
            self.Status.Text = "Help file not found: {0}".format(path)

    def _on_filter_escape(self, sender, args):
        if args.Key == Key.F1:
            self.show_help()
            args.Handled = True
            return
        if args.Key == Key.Escape:
            box = self._filter_box(args.OriginalSource)
            if box is not None and box.Text:
                box.Text = ""
                args.Handled = True

    def _minimize(self):
        self.WindowState = WindowState.Minimized

    def _toggle_max(self):
        if self.WindowState == WindowState.Maximized:
            self.WindowState = WindowState.Normal
        else:
            self.WindowState = WindowState.Maximized

    def _on_state(self, sender, args):
        maxed = self.WindowState == WindowState.Maximized
        self.RootGrid.Margin = Thickness(7) if maxed else Thickness(0)    # a maximized chrome window overhangs the screen
        self.CapMax.Content = "\ue923" if maxed else "\ue922"
        self.CapMax.ToolTip = "Restore" if maxed else "Maximize"

    def pick(self):
        self.say("side")
        self.actions.post(rx_actions.pick, self.on_picked)

    def copy_ids(self):
        Clipboard.SetText(", ".join(str(rx_data.eid_int(e.Id)) for e in self.sel_els))
        self.say("six")

    def _say_for_mode(self):
        if self.mode == "fam":
            self.say("half")
        elif self.mode == "warn":
            self.say("burden" if self.table is not None and self.table.Rows.Count else "still")
        else:
            self.say("village" if self.scope in ("doc", "search") else "where")

    def report(self, msg):
        self.Status.Text = msg

    def _wire(self):
        self.SrcDocument.Checked += lambda s, a: self.set_mode("doc")
        self.SrcFamilies.Checked += lambda s, a: self.set_mode("fam")
        self.SrcSearch.Checked += lambda s, a: self.show_panel(self.PanelSearch)
        self.SrcWarnings.Checked += lambda s, a: self.set_mode("warn")
        self.SrcDatabase.Checked += lambda s, a: self.show_panel(self.PanelDatabase)

        self.ScopeDoc.Checked += lambda s, a: self.load_scope("doc")
        self.ScopeSheet.Checked += lambda s, a: self.load_scope("sheet")
        self.ScopeView.Checked += lambda s, a: self.load_scope("view")
        self.ScopeSched.Checked += lambda s, a: self.load_scope("sched")
        self.GroupBy.SelectionChanged += self.on_groupby
        self.Tree.SelectedItemChanged += self.on_tree
        self._tree_filter = Debounce(300, self.build_tree)
        self.TreeFilter.TextChanged += lambda s, a: self._tree_filter()
        self._list_filter = Debounce(250, self.apply_filter)
        self.ListFilter.TextChanged += lambda s, a: self._list_filter()
        self.ChkHelpers.Click += lambda s, a: self.reload()

        self._multi = Debounce(200, self.inspect_selection)
        self.ElemGrid.SelectionChanged += self.on_grid_selection
        self.ElemGrid.MouseDoubleClick += self.on_grid_double_click
        self.BtnSelect.Click += lambda s, a: self.post_ids(rx_actions.select, self.target_ids())
        self.BtnZoom.Click += lambda s, a: self.post_ids(rx_actions.zoom, self.target_ids())
        self.BtnIsolate.Click += lambda s, a: self.post_ids(rx_actions.isolate, self.target_ids())
        self.BtnPick.Click += lambda s, a: self.pick()
        self.BtnRefresh.Click += lambda s, a: self.reload()
        self.BtnExport.Click += lambda s, a: self.export_csv()
        self.BtnDuplicate.Click += lambda s, a: self.duplicate_type()

        self.InspBack.Click += lambda s, a: self.go_back()
        self.InspSelect.Click += lambda s, a: self.post_ids(rx_actions.select, self.insp_ids())
        self.InspZoom.Click += lambda s, a: self.post_ids(rx_actions.zoom, self.insp_ids())
        self.InspOpen.Click += lambda s, a: self.open_in_view(self.current.Id)
        self.InspCopy.Click += lambda s, a: self.copy_ids()
        self.InspTabs.SelectionChanged += self.on_insp_tab
        self.InspFilter.TextChanged += lambda s, a: self.apply_insp_filter()
        self.ChkEditable.Click += lambda s, a: self.apply_insp_filter()
        self.RelList.MouseDoubleClick += self.on_relation_double_click

        self.DbTree.SelectedItemChanged += self.on_db_tree
        self.DbBack.Click += lambda s, a: self.db_back()
        self.DbTreeFilter.TextChanged += lambda s, a: self.build_db_tree()
        self._db_filter = Debounce(200, lambda: self.db_members.filter((self.DbFilter.Text or "").strip()))
        self.DbFilter.TextChanged += lambda s, a: self._db_filter()

        self.CapHelp.Click += lambda s, a: self.show_help()
        self.CreditLink.RequestNavigate += self._open_link
        self.CapMin.Click += lambda s, a: self._minimize()
        self.CapMax.Click += lambda s, a: self._toggle_max()
        self.CapClose.Click += lambda s, a: self.Close()
        self.StateChanged += self._on_state
        self.AddHandler(ButtonBase.ClickEvent, RoutedEventHandler(self._on_filter_clear))
        self.PreviewKeyDown += self._on_filter_escape

        self._wire_tree_menus()
        self.Closed += self.on_closed

    def _subscribe(self):
        """Revit events - subscribed now (API context), removed via ExternalEvent on close."""
        self._h_view = self.on_view_activated
        self.uiapp.ViewActivated += self._h_view
        self._h_theme = None
        try:
            self._h_theme = self.on_theme_changed
            self.uiapp.ThemeChanged += self._h_theme     # Revit 2024+
        except Exception:
            self._h_theme = None

    def _unsubscribe(self, uiapp):
        try:
            uiapp.ViewActivated -= self._h_view
        except Exception:
            pass
        if self._h_theme is not None:
            try:
                uiapp.ThemeChanged -= self._h_theme
            except Exception:
                pass

    def on_closed(self, sender, args):
        AppDomain.CurrentDomain.SetData(APP_KEY, None)
        self.actions.post(self._unsubscribe)

    def on_theme_changed(self, sender, args):
        self.theme = rx_theme.apply(self)
        self.ThemeLabel.Text = "Follows Revit theme: " + self.theme

    def on_view_activated(self, sender, args):
        try:
            if not args.Document.Equals(self.doc):
                self.report("Switched to {} - reloading.".format(args.Document.Title))
                self.type_counts = None
                self.clear_inspector()
                self.reload()
                self.build_db_tree()
            elif self.mode == "doc" and self.scope in ("view", "sheet"):
                self.report("Active view changed to {} - press Refresh to re-scope.".format(args.CurrentActiveView.Name))
        except Exception:
            pass

    def show_panel(self, panel):
        for p in (self.PanelDocument, self.PanelSearch, self.PanelDatabase):
            p.Visibility = Visibility.Visible if p is panel else Visibility.Collapsed
        if panel is self.PanelSearch:
            self.say("what")
        elif panel is self.PanelDatabase:
            self.say("telling")
        elif self.table is not None:
            self._say_for_mode()

    def post_ids(self, action, ids):
        if not ids:
            self.report("Nothing to act on - highlight rows first." if self.mode == "fam" else "Nothing to act on.")
            return
        self.actions.post(action, ids, self.report)

    def _helpers(self):
        return bool(self.ChkHelpers.IsChecked)

    # ------------------------------------------------------------------ modes and loading
    def _save_mode_state(self):
        if self.table is not None:
            self._mode_state[self.mode] = (self.table, self.indexer, self.ScopeInfo.Text, self.scope, self._tree_cache)

    def set_mode(self, mode):
        self.show_panel(self.PanelDocument)
        if mode == self.mode and self.table is not None:
            return
        self._save_mode_state()
        self.mode = mode
        fam = mode == "fam"
        self._apply_mode_controls()
        self._set_groupings()
        self.clear_inspector()
        cached = self._mode_state.get(mode)
        if cached is not None:
            table, indexer, info, scope, tree_cache = cached
            if mode == "doc":
                self.scope = scope
            self._load_table(table, indexer, info, tree_cache)
        elif fam:
            self.load_families()
        elif mode == "warn":
            self.load_warnings()
        else:
            if self.scope not in DOC_SCOPES:
                self.scope = "doc"
            self.load_scope(self.scope)
        self._say_for_mode()

    def _apply_mode_controls(self):
        """Families lists types only, so everything that acts on instances is switched off there."""
        fam = self.mode == "fam"
        for b in (self.BtnSelect, self.BtnZoom, self.BtnIsolate):
            b.IsEnabled = not fam

        def vis(on):
            return Visibility.Visible if on else Visibility.Collapsed

        self.ScopePills.Visibility = vis(self.mode == "doc")
        self.FamilyBar.Visibility = vis(fam)
        self.FamButtons.Visibility = vis(fam)
        self.WarnBar.Visibility = vis(self.mode == "warn")
        self.ChkHelpers.Visibility = vis(self.mode == "doc")

    def _set_groupings(self, index=0):
        self._loading = True
        self.GroupBy.ItemsSource = [label for label, _ in GROUPINGS[self.mode]]
        self.GroupBy.SelectedIndex = index
        self._loading = False

    def reload(self):
        self.say("where", flush=True)
        self.uidoc = self.uiapp.ActiveUIDocument
        self.doc = self.uidoc.Document
        self._mode_state = {}
        if self.mode == "fam":
            self.type_counts = None
            self.load_families()
        elif self.mode == "warn":
            self.load_warnings()
        elif self.scope == "search":
            self.ScopeDoc.IsChecked = True      # its Checked handler reloads the document
        else:
            self.load_scope(self.scope)

    def load_scope(self, scope):
        self.scope = scope
        Mouse.OverrideCursor = Cursors.Wait
        indexer = rx_data.Indexer(self.doc, self._helpers())
        try:
            active = self.uidoc.ActiveView
            if scope == "view":
                table = indexer.scope_view(active)
                info = "Active view: " + active.Name
            elif scope == "sheet":
                sheet = rx_data.sheet_for_view(self.doc, active)
                if sheet is None:
                    table = indexer.new_table()
                    info = "The active view is not a sheet or on a sheet"
                else:
                    table = indexer.scope_sheet(sheet)
                    info = "Sheet {} - {}  \u00b7  {} viewports".format(sheet.SheetNumber, sheet.Name,
                                                                       sheet.GetAllViewports().Count)
            elif scope == "sched":
                table = indexer.scope_schedules()
                info = "All schedules"
            else:
                table = indexer.scope_document()
                info = "Entire document"
                self.type_counts = indexer.type_counts
        except Exception as ex:
            table = indexer.new_table()
            info = "Could not read scope: " + rx_data.err_text(ex)
        finally:
            Mouse.OverrideCursor = None
        grouping = CONTEXT_GROUPING if scope in ("sheet", "sched") else (
            0 if self.GroupBy.SelectedIndex == CONTEXT_GROUPING else max(0, self.GroupBy.SelectedIndex))
        self._set_groupings(grouping)
        self._load_table(table, indexer, "{}  \u00b7  {:,} elements".format(info, table.Rows.Count))
        self._say_for_mode()

    def load_families(self):
        Mouse.OverrideCursor = Cursors.Wait
        indexer = rx_data.Indexer(self.doc, True)
        try:
            table = indexer.scope_families()
        finally:
            Mouse.OverrideCursor = None
        self._family_ids = {}
        for r in table.Rows:
            fid = int(r["CtxId"])
            if fid > 0:
                self._family_ids[(str(r["Category"]), str(r["Family"]))] = fid
        families = len(set((str(r["Category"]), str(r["Family"])) for r in table.Rows))
        self._load_table(table, indexer, "{:,} types in {:,} families".format(table.Rows.Count, families))
        self._say_for_mode()

    def load_warnings(self):
        Mouse.OverrideCursor = Cursors.Wait
        indexer = rx_data.Indexer(self.doc, True)
        try:
            table, count, kinds, elements = rx_data.scope_warnings(indexer)
        finally:
            Mouse.OverrideCursor = None
        if count:
            info = "{:,} warnings of {:,} kinds  \u00b7  {:,} elements involved".format(count, kinds, elements)
        else:
            info = "No warnings in this model"
        self._load_table(table, indexer, info)
        self._say_for_mode()

    def _load_table(self, table, indexer, info, tree_cache=None):
        self.table = table
        self.indexer = indexer
        self._tree_cache = tree_cache if tree_cache is not None else {}
        self.ScopeInfo.Text = info
        self._set_columns()
        self.ElemGrid.ItemsSource = table.DefaultView
        self.build_tree()

    def _set_columns(self):
        cols = self.ElemGrid.Columns
        fam = self.mode == "fam"

        def show(i, on, header=None):
            cols[i].Visibility = Visibility.Visible if on else Visibility.Collapsed
            if header:
                cols[i].Header = header

        show(C_NAME, not fam)
        show(C_LEVEL, not fam)
        show(C_WORKSET, not fam)
        show(C_MARK, True, "Type Mark" if fam else "Mark")
        if self.mode == "warn":
            show(C_CONTEXT, True, "Warning")
        else:
            ctx = {"view": "View", "sheet": "View", "sched": "Schedule", "search": "Matched on"}.get(self.scope)
            show(C_CONTEXT, fam or (ctx is not None), "Kind" if fam else ctx)
        show(C_COUNT, False)

    def show_results(self, table, indexer, info):
        """Load a search result into the Document tab (tree, list, inspector and editing)."""
        self._save_mode_state()
        self.mode = "doc"
        self.scope = "search"
        self._apply_mode_controls()
        for rb in (self.ScopeDoc, self.ScopeSheet, self.ScopeView, self.ScopeSched):
            rb.IsChecked = False
        self._set_groupings()
        self.clear_inspector()
        self._load_table(table, indexer, "{}  \u00b7  {:,} elements  \u00b7  Refresh returns to the document".format(
            info, table.Rows.Count))
        self.SrcDocument.IsChecked = True
        self.show_panel(self.PanelDocument)

    # ------------------------------------------------------------------ tree
    def _tree_item(self, label, count, bold):
        panel = StackPanel()
        panel.Orientation = Orientation.Horizontal
        name = TextBlock()
        name.Text = label or "(none)"
        if bold:
            name.FontWeight = FontWeights.SemiBold
        panel.Children.Add(name)
        if count is not None:
            num = TextBlock()
            num.Text = "{:,}".format(count)
            num.Margin = Thickness(8, 0, 0, 0)
            num.SetResourceReference(TextBlock.ForegroundProperty, "rx.Muted")
            panel.Children.Add(num)
        item = TreeViewItem()
        item.RequestBringIntoView += self._no_auto_scroll
        item.Header = panel
        if label and len(label) > 40:
            item.ToolTip = label
        return item

    def _grouping_fields(self):
        groupings = GROUPINGS[self.mode]
        i = min(max(0, self.GroupBy.SelectedIndex), len(groupings) - 1)
        return groupings[i][1]

    def build_tree(self):
        if self.table is None:
            return
        fields = self._grouping_fields()
        rows = self._tree_cache.get(fields)
        if rows is None:
            rows = self._tree_cache[fields] = [tuple(str(r[f]) for f in fields) for r in self.table.Rows]
        text = (self.TreeFilter.Text or "").strip().lower()
        if text:
            rows = [r for r in rows if any(text in v.lower() for v in r)]
        root = {}
        for r in rows:
            node = root
            for v in r:
                child = node.get(v)
                if child is None:
                    child = node[v] = [0, {}]
                child[0] += 1
                node = child[1]

        self.Tree.Items.Clear()
        label_all = "All types" if self.mode == "fam" else "All elements"
        everything = self._tree_item(label_all, len(rows), True)
        everything.Tag = (rx_data.contains_expr(list(fields), text) if text else "",
                          label_all + (" matching \u201c{}\u201d".format(text) if text else ""), None)
        self.Tree.Items.Add(everything)
        expand = bool(text) and len(rows) < 3000

        fam_ids = getattr(self, "_family_ids", {}) if self.mode == "fam" else {}
        cat_at = list(fields).index("Category") if "Category" in fields else -1

        def add(items, node, depth, path):
            for key in sorted(node, key=lambda k: k.lower()):
                count, kids = node[key]
                item = self._tree_item(key, count, depth == 0)
                p = path + [key]
                fid = None
                if fam_ids and fields[depth] == "Family" and 0 <= cat_at < depth:
                    fid = fam_ids.get((p[cat_at], key))
                item.Tag = (rx_data.equals_expr(fields[:depth + 1], p), "  \u203a  ".join(v or "(none)" for v in p), fid)
                if expand and kids:
                    item.IsExpanded = True
                items.Add(item)
                if kids:
                    add(item.Items, kids, depth + 1, p)

        add(self.Tree.Items, root, 0, [])
        everything.IsSelected = True

    def on_groupby(self, sender, args):
        if not self._loading and self.table is not None:
            self.build_tree()

    def on_tree(self, sender, args):
        item = self.Tree.SelectedItem
        if item is None or item.Tag is None:
            return
        self._node_filter, crumb, family_id = item.Tag
        self.Crumb.Text = crumb
        self.apply_filter()
        if self.mode == "warn" and self.table is not None and self.table.DefaultView.Count == 1:
            self.ElemGrid.SelectedIndex = 0         # an element leaf: inspect it straight away
        if family_id:
            family = self.doc.GetElement(rx_data.make_eid(family_id))
            if family is not None:
                self.ElemGrid.UnselectAll()
                self.history = []
                self.inspect(family, push=False)

    def apply_filter(self):
        if self.table is None:
            return
        parts = []
        if self._node_filter:
            parts.append("(" + self._node_filter + ")")
        text = (self.ListFilter.Text or "").strip()
        if text:
            parts.append(rx_data.contains_expr(rx_data.TEXT_COLUMNS, text, with_id=True))
        try:
            self.table.DefaultView.RowFilter = " AND ".join(parts)
        except Exception as ex:
            self.report("Filter error: " + rx_data.err_text(ex))
        self.RowCount.Text = "{:,} rows".format(self.table.DefaultView.Count)
        self.update_select_label()

    # ------------------------------------------------------------------ element list
    def _row_ids(self, rows):
        seen, out = set(), []
        for rv in rows:
            n = int(rv["Id"])
            if n not in seen:
                seen.add(n)
                out.append(self.indexer.eids.get(n) or rx_data.make_eid(n))
        return out

    def _elem(self, rv):
        n = int(rv["Id"])
        return self.doc.GetElement(self.indexer.eids.get(n) or rx_data.make_eid(n))

    def target_ids(self):
        """Highlighted rows, or the whole list. Families lists types, which can't be selected in a view."""
        if self.mode == "fam":
            return []
        if self.ElemGrid.SelectedItems.Count:
            return self._row_ids(self.ElemGrid.SelectedItems)
        return self._row_ids(self.table.DefaultView) if self.table is not None else []

    def insp_ids(self):
        return [] if self.mode == "fam" else [e.Id for e in self.sel_els]

    def update_select_label(self):
        if self.mode == "fam":
            self.BtnSelect.Content = "Select in Revit"
            return
        n = self.ElemGrid.SelectedItems.Count
        if n:
            self.BtnSelect.Content = "Select in Revit ({:,})".format(n)
        else:
            count = self.table.DefaultView.Count if self.table is not None else 0
            self.BtnSelect.Content = "Select all listed ({:,})".format(count)

    def on_grid_selection(self, sender, args):
        self.update_select_label()
        n = self.ElemGrid.SelectedItems.Count
        if n == 1:
            el = self._elem(self.ElemGrid.SelectedItem)
            if el is not None:
                self.history = []
                self.inspect(el, push=False)
        elif n > 1:
            self._multi()

    def inspect_selection(self):
        n = self.ElemGrid.SelectedItems.Count
        if n < 2:
            return
        if n > MULTI_CAP:
            self.report("{:,} rows highlighted - edit up to {:,} at a time.".format(n, MULTI_CAP))
            return
        els = [self._elem(rv) for rv in self.ElemGrid.SelectedItems]
        self.inspect_multi([e for e in els if e is not None])

    def on_grid_double_click(self, sender, args):
        rv = self.ElemGrid.SelectedItem
        if rv is None:
            return
        if self.mode == "fam":
            self.report("Edit {} in the panel on the right.".format(rv["Type"]))
            return
        n = int(rv["Id"])
        ctx = int(rv["CtxId"])
        prefer = rx_data.make_eid(ctx) if (ctx > 0 and self.scope != "doc") else None
        self.open_in_view(self.indexer.eids.get(n) or rx_data.make_eid(n), prefer)

    def open_in_view(self, eid, prefer_view_id=None):
        self.say("seeing")
        self.report("Opening a view with the element\u2026")
        self.actions.post(rx_actions.open_in_view, eid, prefer_view_id, self.report)

    def on_picked(self, eid):
        el = self.doc.GetElement(eid)
        if el is None:
            return
        self.history = []
        self.inspect(el, push=False)
        self.ListFilter.Text = str(rx_data.eid_int(eid))
        self.Activate()

    def select_row(self, n):
        """Highlight the row with element id n, widening the tree filter if needed."""
        if self.table is None:
            return
        for attempt in (0, 1):
            for rv in self.table.DefaultView:
                if int(rv["Id"]) == n:
                    self.ElemGrid.SelectedItem = rv
                    self.ElemGrid.ScrollIntoView(rv)
                    return
            if attempt == 0 and self.Tree.Items.Count:
                self.ListFilter.Text = ""
                self.Tree.Items[0].IsSelected = True
                self.apply_filter()

    def duplicate_type(self):
        if self.ElemGrid.SelectedItems.Count != 1:
            self.report("Highlight one type to duplicate.")
            return
        te = self._elem(self.ElemGrid.SelectedItem)
        if not isinstance(te, DB.ElementType):
            return
        name = forms.ask_for_string(default=rx_data.elem_name(te) + " 2", prompt="Name for the new type:",
                                    title="Duplicate type")
        if not name:
            return
        self.actions.post(rx_actions.duplicate_type, self.doc, te.Id, name, self.after_duplicate)

    def after_duplicate(self, new_id):
        self.report("Created type {}. Undo with Ctrl+Z in Revit.".format(rx_data.elem_name(self.doc.GetElement(new_id))))
        self.load_families()
        self.select_row(rx_data.eid_int(new_id))
        self.say("two")

    def export_csv(self, table=None):
        table = table if table is not None else self.table
        if table is None:
            return
        path = forms.save_file(file_ext="csv", default_name="Rover")
        if not path:
            return
        skip = set(["CtxId", "Count"] + rx_data.WARNING_COLUMNS)
        if table is self.table and self.mode == "fam":
            skip.discard("Count")
        if table is self.table and self.mode == "warn":
            skip.update(["Context", "Element"])
            skip.discard("Warning")
        cols = [c.ColumnName for c in table.Columns if c.ColumnName not in skip]

        def cell(v):
            s = "{}".format(v)
            if any(ch in s for ch in ',"\n'):
                s = '"' + s.replace('"', '""') + '"'
            return s

        lines = List[String]()
        lines.Add(",".join(cols))
        for rv in table.DefaultView:
            lines.Add(",".join(cell(rv[c]) for c in cols))
        File.WriteAllLines(path, lines, Encoding.UTF8)
        self.report("Exported {:,} rows to {}".format(lines.Count - 1, path))

    # ------------------------------------------------------------------ inspector
    def clear_inspector(self):
        self.history, self.current, self.sel_els = [], None, []
        self.param_model = None
        self.InspCat.Text = "Nothing selected"
        self.InspTitle.Text = "Select one or more rows to inspect and edit them"
        self.InspIds.Text = ""
        self.ParamList.ItemsSource = None
        self.MemberList.ItemsSource = None
        self.RelList.ItemsSource = None
        self.rel_table = None
        for b in (self.InspSelect, self.InspZoom, self.InspOpen, self.InspCopy, self.InspBack):
            b.IsEnabled = False

    def _cat_label(self, el):
        name = self.indexer.category_of(el) if self.indexer is not None else None
        if not name:
            name = el.Category.Name if el.Category is not None else el.GetType().Name
        return name

    def inspect(self, obj, label=None, push=True):
        if obj is None:
            return
        if push and self.current is not None and self.current is not obj:
            self.history.append(self.current)
            del self.history[:-50]
        self.current = obj
        self._members_for = None
        self._rel_for = None
        self.InspBack.IsEnabled = bool(self.history)

        is_el = isinstance(obj, DB.Element)
        placeable = is_el and self.mode != "fam" and not isinstance(obj, (DB.ElementType, DB.Family))
        self.InspSelect.IsEnabled = placeable
        self.InspZoom.IsEnabled = placeable
        self.InspOpen.IsEnabled = placeable
        self.InspCopy.IsEnabled = is_el
        self.TabParams.IsEnabled = is_el
        self.TabMembers.IsEnabled = True
        self.TabRel.IsEnabled = is_el and self.mode != "fam"
        if self.mode == "fam" and self.InspTabs.SelectedItem is self.TabRel:
            self.InspTabs.SelectedItem = self.TabParams

        if is_el:
            self.say("who")
            self.sel_els = [obj]
            fam, typ = (self.indexer or rx_data.Indexer(self.doc)).type_info(obj)
            name = rx_data.elem_name(obj)
            title = "{} : {}".format(fam, typ) if typ and typ != "(untyped)" else name
            if name and typ and name != typ and typ != "(untyped)":
                title += "  -  " + name
            self.InspCat.Text = "{}  \u00b7  {}".format(self._cat_label(obj), obj.GetType().Name)
            self.InspTitle.Text = title or obj.GetType().Name
            self.InspIds.Text = "ElementId {}  \u00b7  {}".format(rx_data.eid_int(obj.Id), obj.UniqueId)
            self.load_params()
        else:
            self.sel_els = []
            self.param_model = None
            self.InspCat.Text = "Listing" if isinstance(obj, rx_data.Listing) else obj.GetType().FullName
            self.InspTitle.Text = obj.label if isinstance(obj, rx_data.Listing) else (label or obj.GetType().Name)
            self.InspIds.Text = ""
            self.ParamList.ItemsSource = None
            self.RelList.ItemsSource = None
            self.rel_table = None
            if self.InspTabs.SelectedItem is not self.TabMembers:
                self.InspTabs.SelectedItem = self.TabMembers
        self.refresh_tab()

    def inspect_multi(self, els):
        self.say("one")
        self.current = None
        self.history = []
        self.InspBack.IsEnabled = False
        self.sel_els = els
        counts = {}
        for e in els:
            k = self._cat_label(e)
            counts[k] = counts.get(k, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])
        self.InspCat.Text = ", ".join("{} ({:,})".format(k, v) for k, v in top[:4]) + (" \u2026" if len(top) > 4 else "")
        noun = "types" if all(isinstance(e, DB.ElementType) for e in els) else "elements"
        self.InspTitle.Text = "{:,} {} selected".format(len(els), noun)
        self.InspIds.Text = "Edits apply to all of them. Only parameters they all share are listed."
        self.InspSelect.IsEnabled = self.mode != "fam"
        self.InspZoom.IsEnabled = self.mode != "fam"
        self.InspCopy.IsEnabled = True
        self.InspOpen.IsEnabled = False
        self.TabParams.IsEnabled = True
        self.TabMembers.IsEnabled = False
        self.TabRel.IsEnabled = False
        self.InspTabs.SelectedItem = self.TabParams
        self.MemberList.ItemsSource = None
        self.RelList.ItemsSource = None
        self.rel_table = None
        self.load_params()

    def load_params(self):
        Mouse.OverrideCursor = Cursors.Wait
        try:
            self.param_model = rx_edit.ParamModel(self.doc, self.sel_els)
        except Exception as ex:
            self.param_model = None
            self.ParamList.ItemsSource = None
            self.report("Could not read parameters: " + rx_data.err_text(ex))
            return
        finally:
            Mouse.OverrideCursor = None
        self.pending = {}
        self.ParamList.ItemsSource = self.param_model.table.DefaultView
        view = CollectionViewSource.GetDefaultView(self.ParamList.ItemsSource)
        view.GroupDescriptions.Add(PropertyGroupDescription("Group"))
        self.apply_insp_filter()
        need = [s for s in self.param_model.slots.values() if s.editor == "code" and not rx_edit.has_codes(s)]
        if need:
            self.actions.post(rx_actions.probe_codes, self.doc, need)

    def go_back(self):
        if self.history:
            self.inspect(self.history.pop(), push=False)

    def apply_insp_filter(self):
        text = (self.InspFilter.Text or "").strip()
        if self.param_model is not None:
            parts = []
            if text:
                parts.append(rx_data.contains_expr(["Name", "Value", "Group"], text))
            if self.ChkEditable.IsChecked:
                parts.append("[Editor] <> 'ro'")
            self.param_model.table.DefaultView.RowFilter = " AND ".join(parts)
        self.members.filter(text)
        if self.rel_table is not None:
            self.rel_table.DefaultView.RowFilter = rx_data.contains_expr(["Label", "Group"], text) if text else ""

    def on_insp_tab(self, sender, args):
        if args.OriginalSource is self.InspTabs:
            self.refresh_tab()

    def refresh_tab(self):
        tab = self.InspTabs.SelectedItem
        obj = self.current
        if obj is None:
            return
        if tab is self.TabMembers and self._members_for is not obj:
            self._members_for = obj
            self.members.fill(obj)
        elif tab is self.TabRel and self._rel_for is not obj and isinstance(obj, DB.Element):
            self._rel_for = obj
            Mouse.OverrideCursor = Cursors.Wait
            try:
                self.rel_table = rx_data.relations_table(rx_data.relations(self.doc, obj))
            finally:
                Mouse.OverrideCursor = None
            self.RelList.ItemsSource = self.rel_table.DefaultView
            view = CollectionViewSource.GetDefaultView(self.RelList.ItemsSource)
            view.GroupDescriptions.Add(PropertyGroupDescription("Group"))
            self.apply_insp_filter()

    def on_relation_double_click(self, sender, args):
        rv = self.RelList.SelectedItem
        if rv is None:
            return
        target_id = rx_data.make_eid(int(rv["Id"]))
        if rv["Action"] == "open" and isinstance(self.current, DB.Element):
            target = self.doc.GetElement(target_id)
            if isinstance(self.current, DB.View) and isinstance(target, DB.View):
                self.open_in_view(target_id)
            else:
                self.open_in_view(self.current.Id, target_id)
        else:
            self.inspect(self.doc.GetElement(target_id))

    # ------------------------------------------------------------------ parameter editing
    def _slot(self, rv):
        if self.param_model is None:
            return None
        return self.param_model.slots.get(rv["Key"])

    def _live(self, rv):
        """False for a cell left over from a parameter list that has since been rebuilt."""
        try:
            return self.param_model is not None and Object.ReferenceEquals(rv.Row.Table, self.param_model.table)
        except Exception:
            return False

    def on_param_text(self, rv, text, tb):
        if not self._live(rv):
            return
        slot = self._slot(rv)
        if slot is None or slot.editor in ("ro", "bool"):
            return
        if text == rx_edit.VARIES or text == rv["Value"] or self.pending.get(slot.key) == text:
            return
        if slot.editor == "text" and rx_pattern.is_pattern(text):
            if getattr(self, "_pattern_busy", False):
                return                      # the preview dialog took focus from the cell
            self._pattern_busy = True
            try:
                self.pattern_edit(slot, text, rv, tb)
            finally:
                self._pattern_busy = False
            return
        if slot.kind == "P" and len(slot.targets) > 1:
            self.report("Names must differ - use a pattern, e.g. A-[Name]-01 or Door [##]. See Help (F1).")
            tb.Text = rv["Value"]
            return
        value = text
        if slot.editor in ("id", "code"):
            value = rx_edit.resolve(self.doc, slot, text)
            if value is None:
                self.report("No match for \u201c{}\u201d - pick it from the drop-down.".format(text))
                tb.Text = rv["Value"]
                return
        self.pending[slot.key] = text
        self.post_edit(slot, value, text)

    def on_param_bool(self, rv):
        if not self._live(rv):
            return
        slot = self._slot(rv)
        if slot is None or slot.editor != "bool":
            return
        value = rv["BoolVal"] != "1"
        self.post_edit(slot, value, "Yes" if value else "No")

    def on_param_options(self, rv):
        if not self._live(rv):
            return None
        slot = self._slot(rv)
        if slot is None or slot.editor not in ("id", "code"):
            return None
        Mouse.OverrideCursor = Cursors.Wait
        try:
            labels = rx_edit.option_labels(self.doc, slot)
        finally:
            Mouse.OverrideCursor = None
        if not labels:
            self.report("No choices found for {} - type a value and press Enter.".format(slot.name))
        return labels

    def on_param_choice(self, rv, label, combo):
        if not self._live(rv):
            return
        slot = self._slot(rv)
        if slot is None or slot.editor not in ("id", "code") or self.pending.get(slot.key) == label:
            return
        value = rx_edit.option_value(self.doc, slot, label)
        if value is None:
            self.report("\u201c{}\u201d is not a valid choice for {}.".format(label, slot.name))
            return
        self.pending[slot.key] = label
        self.post_edit(slot, value, label)

    def on_param_pick(self, rv):
        slot = self._slot(rv)
        if slot is None or slot.editor != "id":
            return
        Mouse.OverrideCursor = Cursors.Wait
        try:
            opts = rx_edit.choices(self.doc, slot)
        finally:
            Mouse.OverrideCursor = None
        if not opts:
            self.report("No choices found for {} - type an Element Id instead.".format(slot.name))
            return
        picked = forms.SelectFromList.show([label for label, _ in opts], title="Set " + slot.name, button_name="Set")
        if picked:
            self.post_edit(slot, dict(opts)[picked], picked)

    def _list_order(self):
        """Element id int -> position in the list as currently sorted and filtered."""
        order = {}
        try:
            for i, rv in enumerate(self.table.DefaultView):
                order[int(rv["Id"])] = i
        except Exception:
            pass
        return order

    def _current_text(self, slot, el):
        if slot.kind == "P":
            return rx_data.elem_name(el)
        return rx_pattern._param_text(self.doc, slot.find(el))

    def pattern_edit(self, slot, pattern, rv, tb):
        """Work out a value per element from a [token] pattern, preview, then write them in one transaction."""
        els = [e for e in (self.doc.GetElement(i) for i in slot.targets) if e is not None]
        if slot.kind != "T":
            order = self._list_order()
            big = len(order) + 1
            els.sort(key=lambda e: order.get(rx_data.eid_int(e.Id), big))
        try:
            pairs = rx_pattern.expand(self.doc, pattern, els, lambda e: self._current_text(slot, e))
        except ValueError as ex:
            self.report("Pattern: {}".format(ex))
            tb.Text = rv["Value"]
            return
        changes = [(e, v) for e, v in pairs if v != self._current_text(slot, e)]
        if not changes:
            self.report("The pattern gives the current values - nothing to change.")
            tb.Text = rv["Value"]
            return
        dup = rx_pattern.duplicates(pairs)
        if dup and slot.kind == "P":
            self.report("Pattern gives the same name more than once (\u201c{}\u201d) - add [#] or [Id] to keep them apart."
                        .format(dup[0]))
            tb.Text = rv["Value"]
            return
        if len(pairs) > 1:
            lines = ["{}  \u2192  {}".format(self._current_text(slot, e) or "(empty)", v) for e, v in changes[:12]]
            if len(changes) > 12:
                lines.append("\u2026 and {} more".format(len(changes) - 12))
            if dup:
                lines.append("")
                lines.append("Note: \u201c{}\u201d is produced more than once.".format(dup[0]))
            if not forms.alert("\n".join(lines),
                               title="Rover",
                               sub_msg="Set {} on {} of {} elements? One undo (Ctrl+Z) reverses it.".format(
                                   slot.name, len(changes), len(pairs)),
                               ok=False, yes=True, no=True):
                tb.Text = rv["Value"]
                self.report("Pattern edit cancelled.")
                return
        per = dict((rx_data.eid_int(e.Id), v) for e, v in changes)
        self.pending[slot.key] = pattern
        self.report("Setting {} from pattern {} on {} element{}\u2026".format(
            slot.name, pattern, len(per), "" if len(per) == 1 else "s"))
        self.actions.post(rx_actions.apply_edit, self.doc, slot, None, self.after_edit, per)

    def post_edit(self, slot, value, shown):
        n = len(slot.targets)
        what = "{} type{}".format(n, "" if n == 1 else "s") if slot.kind == "T" else \
            "{} element{}".format(n, "" if n == 1 else "s")
        self.report("Setting {} = {} on {}\u2026".format(slot.name, shown, what))
        self.actions.post(rx_actions.apply_edit, self.doc, slot, value, self.after_edit)

    def after_edit(self, slot, ok, errors):
        self.pending = {}
        msg = "{} set on {} of {}.".format(slot.name, ok, len(slot.targets))
        if ok and slot.kind == "T":
            msg += " Type parameter - every instance of the type changed."
        if errors:
            msg += "  \u26a0 " + errors[0] + ("  (+{} more)".format(len(errors) - 1) if len(errors) > 1 else "")
        if ok:
            msg += "  Undo with Ctrl+Z in Revit."
        self.refresh_after_edit(slot.targets if slot.kind == "I" else None)
        self.report(msg)
        if not ok:
            self.say("pushed")
        else:
            self.say("free" if slot.kind == "P" else "hook")

    def after_property_change(self, obj):
        if isinstance(obj, DB.Element) and any(e.Id == obj.Id for e in self.sel_els):
            self.refresh_after_edit([obj.Id])

    def refresh_after_edit(self, ids=None):
        """Re-read the edited elements into the inspector and their list rows."""
        self.sel_els = [e for e in (self.doc.GetElement(x.Id) for x in self.sel_els) if e is not None]
        if len(self.sel_els) == 1 and self.current is not None:
            history = self.history
            self.inspect(self.sel_els[0], push=False)
            self.history = history
            self.InspBack.IsEnabled = bool(history)
        elif self.sel_els:
            self.load_params()
        if self.table is None or self.indexer is None:
            return
        self.indexer._types.clear()
        if ids is None:
            view = self.table.DefaultView
            rows = [rv.Row for rv in view] if view.Count <= 3000 else []
        else:
            nums = [str(rx_data.eid_int(i)) for i in ids][:2000]
            rows = list(self.table.Select("[Id] IN ({})".format(",".join(nums)))) if nums else []
        fam = self.mode == "fam"
        for r in rows:
            el = self.doc.GetElement(rx_data.make_eid(int(r["Id"])))
            if el is not None:
                self.indexer.update_row(r, el, fam)
        self._tree_cache = {}

    # ------------------------------------------------------------------ settings & database
    def build_db_tree(self):
        self.DbTree.Items.Clear()
        self._db_roots = rx_data.db_roots(self.uiapp, self.doc)
        text = (self.DbTreeFilter.Text or "").strip().lower()
        sections = {}
        for i, (section, label, factory) in enumerate(self._db_roots):
            if text and text not in label.lower() and text not in section.lower():
                continue
            parent = sections.get(section)
            if parent is None:
                parent = self._tree_item(section, None, True)
                parent.IsExpanded = True
                sections[section] = parent
                self.DbTree.Items.Add(parent)
            leaf = TreeViewItem()
            leaf.RequestBringIntoView += self._no_auto_scroll
            leaf.Header = label
            leaf.Tag = i
            parent.Items.Add(leaf)

    def on_db_tree(self, sender, args):
        item = self.DbTree.SelectedItem
        if item is None or item.Tag is None:
            return
        section, label, factory = self._db_roots[int(item.Tag)]
        Mouse.OverrideCursor = Cursors.Wait
        try:
            obj = factory()
        except Exception as ex:
            self.report("{}: {}".format(label, rx_data.err_text(ex)))
            return
        finally:
            Mouse.OverrideCursor = None
        self.db_history = []
        self.db_current = None
        self.db_show(obj, label)

    def db_show(self, obj, label=None, push=True):
        if obj is None:
            return
        if push and self.db_current is not None:
            self.db_history.append(self.db_current)
        self.db_current = (obj, label)
        self.DbBack.IsEnabled = bool(self.db_history)
        if isinstance(obj, rx_data.Listing):
            self.DbTitle.Text = obj.label
            self.DbClass.Text = "{:,} items".format(len(obj.items))
        else:
            self.DbTitle.Text = label or obj.GetType().Name
            if isinstance(obj, DB.Element):
                self.DbTitle.Text = "{}  [{}]".format(rx_data.elem_name(obj) or label, rx_data.eid_int(obj.Id))
            self.DbClass.Text = obj.GetType().FullName
        Mouse.OverrideCursor = Cursors.Wait
        try:
            self.db_members.fill(obj)
        finally:
            Mouse.OverrideCursor = None

    def db_back(self):
        if self.db_history:
            obj, label = self.db_history.pop()
            self.db_show(obj, label, push=False)
