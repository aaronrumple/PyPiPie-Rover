# -*- coding: utf-8 -*-
"""Rover - Search panel controller (query builder, saved searches, results)."""
from __future__ import unicode_literals
import time

from System.Windows import Thickness, GridLength, GridUnitType, VerticalAlignment, HorizontalAlignment, FontWeights
from System.Windows.Controls import (Grid, ColumnDefinition, ComboBox, TextBlock, Button, ListBoxItem)
from System.Windows.Input import Cursors, Mouse, Key

from pyrevit import forms

import rx_data
import rx_actions
import rx_search
from rx_util import Debounce

MATCH_LABELS = ["All conditions (AND)", "Any condition (OR)"]
VALUE_FIELDS_WITH_LISTS = set(["Category", "Level", "Workset", "Family", "Type", "Class", "Parameter"])


class CondRow(object):
    """One condition: [join] [field] [parameter] [operator] [value] [x]."""

    def __init__(self, panel, cond):
        self.panel = panel
        win = panel.win
        g = Grid()
        g.Margin = Thickness(0, 0, 0, 6)
        for w in (56, 170, 230, 150, None, 34):
            cd = ColumnDefinition()
            cd.Width = GridLength(w) if w else GridLength(1, GridUnitType.Star)
            g.ColumnDefinitions.Add(cd)

        self.join = TextBlock()
        self.join.FontWeight = FontWeights.SemiBold
        self.join.VerticalAlignment = VerticalAlignment.Center
        self.join.HorizontalAlignment = HorizontalAlignment.Right
        self.join.Margin = Thickness(0, 0, 10, 0)
        self.join.SetResourceReference(TextBlock.ForegroundProperty, "rx.Muted")

        self.field = ComboBox()
        self.field.ItemsSource = rx_search.FIELDS
        self.param = ComboBox()
        self.param.IsEditable = True
        self.param.ToolTip = "Parameter name (instance or type). Open the list for names found in this scope."
        self.op = ComboBox()
        self.op.ItemsSource = rx_search.OPS
        self.value = ComboBox()
        self.value.IsEditable = True
        self.value.ToolTip = "Value. Open the list for values found in the model. Numbers use project units."
        self.remove = Button()
        self.remove.Content = "\u00d7"
        self.remove.Style = win.FindResource("rx.Btn")
        self.remove.Padding = Thickness(0)
        self.remove.ToolTip = "Remove condition"

        for i, ctl in enumerate((self.join, self.field, self.param, self.op, self.value, self.remove)):
            if i in (1, 2, 3, 4):
                ctl.Margin = Thickness(0, 0, 6, 0)
            Grid.SetColumn(ctl, i)
            g.Children.Add(ctl)
        self.element = g

        self.field.SelectedIndex = _index(rx_search.FIELDS, cond.get("field"), 0)
        self.op.SelectedIndex = _index(rx_search.OPS, cond.get("op"), 0)
        self.param.Text = cond.get("param") or ""
        self.value.Text = cond.get("value") or ""
        self._sync_enabled()

        self.field.SelectionChanged += lambda s, a: self._sync_enabled()
        self.op.SelectionChanged += lambda s, a: self._sync_enabled()
        self.param.DropDownOpened += self._suggest_params
        self.value.DropDownOpened += self._suggest_values
        self.value.KeyDown += self._enter_runs
        self.param.KeyDown += self._enter_runs
        self.remove.Click += lambda s, a: panel.remove_row(self)

    def _sync_enabled(self):
        self.param.IsEnabled = self.field.SelectedItem == "Parameter"
        self.value.IsEnabled = self.op.SelectedItem not in rx_search.NO_VALUE_OPS

    def _enter_runs(self, sender, args):
        if args.Key == Key.Return:
            self.panel.run()

    def _keep_text(self, combo, items):
        text = combo.Text
        combo.ItemsSource = items
        combo.Text = text

    def _suggest_params(self, sender, args):
        self._keep_text(self.param, self.panel.param_names())

    def _suggest_values(self, sender, args):
        self._keep_text(self.value, self.panel.value_suggestions(self.field.SelectedItem, self.param.Text))

    def to_dict(self):
        return {"field": self.field.SelectedItem or "Category", "param": (self.param.Text or "").strip(),
                "op": self.op.SelectedItem or "contains", "value": self.value.Text or ""}


def _index(items, value, default):
    try:
        return list(items).index(value)
    except ValueError:
        return default


class SearchPanel(object):

    def __init__(self, win):
        self.win = win
        self.rows = []
        self.searches = rx_search.load_searches()
        self.current = None          # index into self.searches being edited
        self.table = None
        self.indexer = None
        self._samples = {}
        self._loading = False

        win.MatchMode.ItemsSource = MATCH_LABELS
        win.SearchScope.ItemsSource = [label for _, label in rx_search.SCOPES]
        win.MatchMode.SelectionChanged += lambda s, a: self._update_joins()
        win.SearchScope.SelectionChanged += lambda s, a: self._samples.clear()

        win.BtnAddCond.Click += lambda s, a: self.add_row({"field": "Parameter", "op": "equals"})
        win.BtnRunSearch.Click += lambda s, a: self.run()
        win.BtnFindValue.Click += lambda s, a: self.find_value()
        win.FindValueBox.KeyDown += self._find_on_enter

        win.SavedList.SelectionChanged += self.on_saved_selected
        win.SavNew.Click += lambda s, a: self.new_search()
        win.SavSave.Click += lambda s, a: self.save_current()
        win.SavDelete.Click += lambda s, a: self.delete_current()
        win.SavImport.Click += lambda s, a: self.import_file()
        win.SavExport.Click += lambda s, a: self.export_file()

        win.ResGrid.MouseDoubleClick += self.on_result_double_click
        win.ResGrid.SelectionChanged += lambda s, a: self._update_result_buttons()
        win.ResSelect.Click += lambda s, a: win.post_ids(rx_actions.select, self.result_ids())
        win.ResAddSel.Click += lambda s, a: win.post_ids(rx_actions.add_to_selection, self.result_ids())
        win.ResIsolate.Click += lambda s, a: win.post_ids(rx_actions.isolate, self.result_ids())
        win.ResExport.Click += lambda s, a: win.export_csv(self.table)
        self._res_filter = Debounce(250, self.apply_result_filter)
        win.ResFilter.TextChanged += lambda s, a: self._res_filter()
        win.SavedFilter.TextChanged += lambda s, a: self.refresh_saved()
        win.ResToDoc.Click += lambda s, a: self.send_to_document()

        self.refresh_saved()
        if self.searches:
            self.win.SavedList.SelectedIndex = 0
        else:
            self.load_query(rx_search.blank_query())

    @property
    def doc(self):
        return self.win.doc

    # ------------------------------------------------------------------ editor
    def add_row(self, cond):
        row = CondRow(self, cond)
        self.rows.append(row)
        self.win.CondPanel.Children.Add(row.element)
        self._update_joins()
        return row

    def remove_row(self, row):
        if row in self.rows:
            self.rows.remove(row)
            self.win.CondPanel.Children.Remove(row.element)
        if not self.rows:
            self.add_row({"field": "Category", "op": "equals"})
        self._update_joins()

    def _update_joins(self):
        word = "OR" if self.win.MatchMode.SelectedIndex == 1 else "AND"
        for i, row in enumerate(self.rows):
            row.join.Text = "WHERE" if i == 0 else word

    def load_query(self, q):
        w = self.win
        w.SearchName.Text = q.get("name", "")
        w.MatchMode.SelectedIndex = 1 if q.get("match") == "any" else 0
        w.SearchScope.SelectedIndex = _index([k for k, _ in rx_search.SCOPES], q.get("scope", "doc"), 0)
        w.ChkTypes.IsChecked = bool(q.get("include_types"))
        w.ChkCase.IsChecked = bool(q.get("case"))
        w.CondPanel.Children.Clear()
        self.rows = []
        for c in q.get("conditions") or [{"field": "Category", "op": "equals"}]:
            self.add_row(c)
        self._samples.clear()

    def to_query(self):
        w = self.win
        return {"name": (w.SearchName.Text or "").strip() or "Untitled search",
                "match": "any" if w.MatchMode.SelectedIndex == 1 else "all",
                "scope": rx_search.SCOPES[max(0, w.SearchScope.SelectedIndex)][0],
                "include_types": bool(w.ChkTypes.IsChecked),
                "case": bool(w.ChkCase.IsChecked),
                "conditions": [r.to_dict() for r in self.rows]}

    # ------------------------------------------------------------------ suggestions
    def _sample(self):
        q = self.to_query()
        cat_id = rx_search.Query(self.doc, rx_data.Indexer(self.doc), q).category_prefilter()
        key = rx_data.eid_int(cat_id) if cat_id is not None else "all"
        if key not in self._samples:
            self._samples[key] = rx_search.sample_elements(self.doc, cat_id, 600 if cat_id is not None else 1500)
        return self._samples[key]

    def param_names(self):
        try:
            return rx_search.parameter_names(self.doc, self._sample())
        except Exception as ex:
            self.win.report("Could not list parameters: " + rx_data.err_text(ex))
            return []

    def value_suggestions(self, field, pname):
        doc = self.doc
        try:
            if field == "Category":
                return rx_search.category_names(doc)
            if field == "Level":
                return rx_search.level_names(doc)
            if field == "Workset":
                return rx_search.workset_names(doc)
            if field == "Parameter" and (pname or "").strip():
                return rx_search.parameter_values(doc, self._sample(), pname.strip())
            if field in ("Family", "Type", "Class"):
                idx = rx_data.Indexer(doc)
                pick = {"Family": lambda e: idx.type_info(e)[0], "Type": lambda e: idx.type_info(e)[1],
                        "Class": lambda e: e.GetType().Name}[field]
                return sorted(set(pick(e) for e in self._sample()), key=lambda s: s.lower())
        except Exception as ex:
            self.win.report("Could not list values: " + rx_data.err_text(ex))
        return []

    # ------------------------------------------------------------------ running
    def run(self, q=None):
        q = q or self.to_query()
        w = self.win
        list_ids = None
        if q.get("scope") == "list":
            list_ids = w._row_ids(w.table.DefaultView) if w.table is not None else []
        w.say("want", flush=True)
        Mouse.OverrideCursor = Cursors.Wait
        t0 = time.time()
        try:
            table, idx, scanned = rx_search.run(self.doc, w.uidoc, q, list_ids, bool(w.ChkHelpers.IsChecked))
        except Exception as ex:
            Mouse.OverrideCursor = None
            w.report("Search failed: " + rx_data.err_text(ex))
            return
        finally:
            Mouse.OverrideCursor = None
        self.table, self.indexer = table, idx
        self.last_name = q.get("name", "")
        w.ResGrid.ItemsSource = table.DefaultView
        self.apply_result_filter()
        w.say("information" if table.Rows.Count else "wont")
        w.ResultCount.Text = "{:,} found  \u00b7  {:,} scanned  \u00b7  {:.1f} s  \u00b7  {}".format(
            table.Rows.Count, scanned, time.time() - t0, q.get("name", ""))
        self._update_result_buttons()
        w.report("Double-click a result to open a view showing it.")

    def apply_result_filter(self):
        if self.table is None:
            return
        text = (self.win.ResFilter.Text or "").strip()
        self.table.DefaultView.RowFilter = rx_data.contains_expr(rx_data.TEXT_COLUMNS, text, with_id=True) if text else ""

    def find_value(self):
        text = (self.win.FindValueBox.Text or "").strip()
        if not text:
            return
        q = self.to_query()
        q["name"] = "Find \u201c{}\u201d".format(text)
        q["match"] = "all"
        q["conditions"] = [{"field": "Any parameter value", "param": "", "op": "contains", "value": text}]
        self.run(q)

    def _find_on_enter(self, sender, args):
        if args.Key == Key.Return:
            self.find_value()

    def result_ids(self):
        if self.table is None:
            return []
        grid = self.win.ResGrid
        rows = grid.SelectedItems if grid.SelectedItems.Count else self.table.DefaultView
        seen, out = set(), []
        for rv in rows:
            n = int(rv["Id"])
            if n not in seen:
                seen.add(n)
                out.append(self.indexer.eids.get(n) or rx_data.make_eid(n))
        return out

    def _update_result_buttons(self):
        grid = self.win.ResGrid
        if self.table is None:
            return
        if grid.SelectedItems.Count:
            self.win.ResSelect.Content = "Select in Revit ({:,})".format(len(self.result_ids()))
        else:
            self.win.ResSelect.Content = "Select all results ({:,})".format(self.table.Rows.Count)

    def on_result_double_click(self, sender, args):
        rv = self.win.ResGrid.SelectedItem
        if rv is None:
            return
        n = int(rv["Id"])
        ctx = int(rv["CtxId"])
        prefer = rx_data.make_eid(ctx) if ctx > 0 else None
        self.win.open_in_view(self.indexer.eids.get(n) or rx_data.make_eid(n), prefer)

    def send_to_document(self):
        if self.table is None:
            return
        self.win.show_results(self.table, self.indexer, "Search: " + getattr(self, "last_name", ""))

    # ------------------------------------------------------------------ saved searches
    def refresh_saved(self, select=None):
        lb = self.win.SavedList
        self._loading = True
        lb.Items.Clear()
        text = (self.win.SavedFilter.Text or "").strip().lower()
        for i, q in enumerate(self.searches):
            if text and text not in q.get("name", "").lower():
                continue
            item = ListBoxItem()
            item.Content = q.get("name", "Untitled search")
            item.Tag = i
            item.ToolTip = "{} condition(s), {}".format(len(q.get("conditions", [])),
                                                       dict(rx_search.SCOPES).get(q.get("scope", "doc"), ""))
            lb.Items.Add(item)
        self._loading = False
        if select is not None:
            for item in lb.Items:
                if int(item.Tag) == select:
                    lb.SelectedItem = item
                    break

    def on_saved_selected(self, sender, args):
        if self._loading:
            return
        item = self.win.SavedList.SelectedItem
        if item is None:
            return
        self.current = int(item.Tag)
        self.load_query(self.searches[self.current])

    def new_search(self):
        self.current = None
        self.win.SavedList.SelectedIndex = -1
        self.load_query(rx_search.blank_query())
        self.win.SearchName.Focus()

    def save_current(self):
        q = self.to_query()
        target = self.current
        names = [s.get("name") for s in self.searches]
        if target is None or names[target] != q["name"]:
            target = names.index(q["name"]) if q["name"] in names else None
        if target is None:
            self.searches.append(q)
            target = len(self.searches) - 1
        else:
            self.searches[target] = q
        try:
            rx_search.save_searches(self.searches)
        except Exception as ex:
            self.win.report("Could not save searches: " + rx_data.err_text(ex))
            return
        self.current = target
        self.refresh_saved(select=target)
        self.win.report("Saved \u201c{}\u201d to {}".format(q["name"], rx_search.STORE_PATH))

    def delete_current(self):
        if self.current is None:
            return
        name = self.searches[self.current].get("name", "")
        if not forms.alert("Delete the saved search \u201c{}\u201d?".format(name), yes=True, no=True):
            return
        del self.searches[self.current]
        rx_search.save_searches(self.searches)
        self.current = None
        self.refresh_saved()
        self.new_search()

    def import_file(self):
        path = forms.pick_file(file_ext="json")
        if not path:
            return
        try:
            incoming = rx_search.import_searches(path)
        except Exception as ex:
            self.win.report("Import failed: " + rx_data.err_text(ex))
            return
        names = [s.get("name") for s in self.searches]
        for q in incoming:
            if q.get("name") in names:
                self.searches[names.index(q.get("name"))] = q
            else:
                self.searches.append(q)
                names.append(q.get("name"))
        rx_search.save_searches(self.searches)
        self.refresh_saved()
        self.win.report("Imported {} search(es).".format(len(incoming)))

    def export_file(self):
        path = forms.save_file(file_ext="json", default_name="Rover searches")
        if not path:
            return
        rx_search.save_searches(self.searches, path)
        self.win.report("Exported {} search(es) to {}".format(len(self.searches), path))
