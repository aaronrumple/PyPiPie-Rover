<p align="center"><img src="Rover.tab/Browse.panel/Rover.pushbutton/rover.png" width="128" alt="Rover"></p>

# Rover

*"Where am I?" "In the Village."*

Crafted by: [Aaron Rumple, AIA](https://www.linkedin.com/in/aaronrumple/)

Rover is a [pyRevit](https://github.com/pyrevitlabs/pyRevit) extension for Autodesk Revit: one modeless window to browse, search, inspect and edit the model. Think Revit Lookup crossed with Ideate Explorer, written in IronPython. Part of the PyPiPie tools.

- **Follows Revit's theme:** dark or light, and switches when Revit does.
- **Modeless:** keep it open while you work in Revit.
- **Edit parameters in place:** instance and type parameters, for one element or 2,000 at once.

## Features

| Tab | What it does |
|---|---|
| **Document** | Browse the entire document, the active sheet (with every viewport's view), the active view, or all schedules. Every category is included: model, annotation, Views, Sheets, Schedules, Groups, Phases, Revisions, plus Families, patterns, filters and parameters. Group by category, family, type, level, workset or view. |
| **Families** | Every loadable family, system family and internal type, grouped *Category › Family › Type*. Instances are never listed. Edit, rename and duplicate types. |
| **Search** | A query builder (category, family, type, name, level, workset, class, any parameter, with 13 operators and project units), plus *Find value anywhere*. Searches can be saved, imported and exported as JSON. |
| **Warnings** | Every Revit warning, grouped *Warning › occurrence › affected element*. Fix elements right in the inspector. |
| **Settings & Database** | A Revit Lookup-style snoop of Application, Document, Units, Worksets, Object Styles, Materials, Extensible Storage, updaters and more. |

### Everywhere

- **Inspector:** shows parameters (**I** marks instance, **T** marks type), API members and relations.
- **Editing:**
  - Text and number cells use project units.
  - Yes/No parameters are checkboxes.
  - Coded parameters (Function, Location Line…) and element parameters (Type, Level, Phase, Material, Workset…) have drop-downs.
  - Read-only parameters are never written.
  - **Patterns** give each element its own value: `A-[Name]-01`, `D[##] [Name|upper]`, `[Sheet Number] - [Name]`, `[Name/old/new]`. Use them to bulk-rename, renumber sheets or set Marks. A preview shows the changes first.
  - Each edit is one undoable transaction, *Rover: Set …*.
- **Select, Zoom, Isolate, Pick** in Revit. **Double-click** a row to open a view that shows the element.
- **Filter box on every pane**, cleared with × or Esc.
- **Tree right-click menu:** Expand All, Collapse All, Isolate, Show All, Isolate Selected in View, Show All in View.
- **CSV export** of any list.
- **Help:** press **F1** or click **?** in the title bar, or press **F1** while hovering the ribbon button, to open the full HTML guide (`Rover_help.html`).

## Install

### pyRevit Extension Manager / CLI

```
pyrevit extend ui Rover https://github.com/aaronrumple/PyPiPie-Rover.git
```

Then click **pyRevit › Reload**.

### Manual

1. Download or clone this repository.
2. Rename the folder to `Rover.extension`.
3. Put it in `%APPDATA%\pyRevit\Extensions`, or add its parent folder under *pyRevit › Settings › Custom Extension Directories*.
4. Reload pyRevit.

The button is at **Rover tab › Browse › Rover**.

**Requirements:** pyRevit with the IronPython engine. Theme following needs Revit 2024 or later (the version that added the dark UI).

## Repository layout

```
extension.json                     pyRevit extension manifest
Rover.tab/Browse.panel/Rover.pushbutton/
  script.py, bundle.yaml           button entry point (persistent engine)
  RoverWindow.xaml                 layout and styles (all colors are DynamicResources)
  Themes/Dark.xaml, Light.xaml     theme brushes
  Rover_help.html                  user guide (F1 in Rover and on the ribbon button)
  rx_window.py                     window logic: tabs, trees, filters, inspector, editing
  rx_data.py                       element/type index, parameters, reflection, relations
  rx_edit.py                       editable parameter model, drop-down choices, writes
  rx_actions.py                    ExternalEvent queue: select, zoom, isolate, open view, edits
  rx_views.py                      fast "which views show this element" screening
  rx_search.py, rx_search_ui.py    search engine and Search tab
  rx_theme.py                      Revit theme detection
  rx_quotes.py                     title-bar line for each task
  rx_util.py                       debounce helper
  rx_splash.py, splash.png         splash screen while the document loads (replace splash.png to customize)
```

Saved searches are stored in `%APPDATA%\pyRevit\Rover\searches.json`.

### Notes for contributors

- **Python files must be pure ASCII:** escape anything else as `\uXXXX` and keep `from __future__ import unicode_literals`. IronPython's codec handling is unreliable otherwise.
- **All Revit writes go through `rx_actions.ActionQueue`** (an ExternalEvent), because the window is modeless.

## License

GPL-3.0. See [LICENSE](LICENSE).

© 2026 [Aaron Rumple, AIA](https://www.linkedin.com/in/aaronrumple/)

*Be seeing you.*
