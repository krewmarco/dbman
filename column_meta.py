"""The column-metadata virtual table: `e` on a selected column.

Completes the select-mode vocabulary. `e` already means "edit the selected
thing" - field mode edits the cell, row mode edits the row (or hands it to
the provider's own app); column mode was the one slot with nothing behind
it, so every column-level property had to claim a top-level key of its own
(`z` hide, `w` width, `o` sort). That namespace is finite and was filling
up; here, adding a property is adding a row.

Distinct from editing the *table's* schema, which is what Schema mode (`m`)
shows - these are dbman's own display settings for one column, persisted to
dbman.sqlite via ViewSettingsStore, plus the provider facts that explain
why some of them are there at all.

See PLANNING_space-vs-edit-keybinding.md, whose follow-on section proposed
exactly this slot for per-column configuration.
"""
from rich.text import Text

from providers.base import Column, ColumnOption
from virtual_table import RowEdit, VirtualRow, VirtualTable

YES = Text("yes", style="green")
NO = Text("no", style="grey62")
NONE = Text("—", style="grey62")

# Fixed-choice settings edit like an enum column: `e` picks from these.
YES_NO = (ColumnOption("yes", "green"), ColumnOption("no", "gray"))
SORT_OPTIONS = (ColumnOption("ascending"), ColumnOption("descending"), ColumnOption("none"))


class ColumnMetadataTable(VirtualTable):
    """Rows are one column's properties. `e` sets the selected one - a pick
    for fixed choices, typed text for Width - and `space` is its quick
    action (see open_row).

    Writes straight through to the ViewSettingsStore on each change, rather
    than batching until save, so it behaves like the single-key actions it
    supersedes (`z`/`w`/`o` each persist immediately). result() reports
    whether anything changed so the caller knows to re-render the table.
    """

    hint = "e edits · space toggles or cycles · escape closes"
    commits_immediately = True

    # Rows the provider owns: facts about the column, not preferences.
    # Shown because they're the context for the editable rows below (a
    # Colored row only makes sense once you can see the column has options)
    # and because a read-only column is worth knowing before you try to edit.
    def __init__(self, column: Column, item_name: str, view_settings_store,
                 visible_column_count: int, auto_width=None, fit_width=None):
        self.column = column
        self.item_name = item_name
        self.store = view_settings_store
        self.settings = view_settings_store.get(item_name)
        self.visible_column_count = visible_column_count
        # What this column would be sized at with no override - shown next
        # to "auto" so there's a number to start from, which is the one
        # affordance the retired `w` dialog had that this didn't.
        self.auto_width = auto_width
        # The longest loaded value's width, for the same reason: "fit (87)"
        # says what typing `fit` would get you before you type it.
        self.fit_width = fit_width
        self.title = f"Column: {column.name}"
        self.changed = False
        self.error = None

    def columns(self) -> list[Column]:
        return [Column("Property", "text"), Column("Value", "text")]

    def _width_value(self):
        strategy = self._width_name()
        # auto and fit show the width they currently come to; a manual
        # width is already a number.
        number = {"auto": self.auto_width, "fit": self.fit_width}.get(strategy)
        if number is None:
            return Text(strategy)
        return Text.assemble(strategy, (f" ({number})", "grey62"))

    def _sort_value(self):
        name = self._sort_name()
        return NONE if name == "none" else Text(name)

    def rows(self) -> list[VirtualRow]:
        col = self.column
        rows = [
            VirtualRow(None, [Text("Type", style="grey62"), Text(str(col.type_name), style="grey62")]),
            VirtualRow(None, [Text("Editable", style="grey62"), (NO if col.read_only else YES)]),
        ]
        if col.options:
            rows.append(VirtualRow(None, [
                Text("Options", style="grey62"), Text(str(len(col.options)), style="grey62"),
            ]))
        rows.append(VirtualRow("hidden", [Text("Hidden"), YES if col.name in self.settings.hidden else NO]))
        if col.options:
            # Only offered where it does something: colors come from an
            # option set, so a plain text column has nothing to turn off.
            rows.append(VirtualRow("colored", [
                Text("Colored"), NO if col.name in self.settings.no_color else YES,
            ]))
        rows.append(VirtualRow("width", [Text("Width"), self._width_value()]))
        rows.append(VirtualRow("sort", [Text("Sort"), self._sort_value()]))
        if self.error:
            rows.append(VirtualRow(None, [Text("!", style="red"), Text(self.error, style="red")]))
        return rows

    def _save(self):
        self.store.save(self.item_name, self.settings)
        self.changed = True

    # `space` is each row's quick action: flip a yes/no, cycle the sort,
    # swing Width between its two automatic strategies. `e` (edit_row)
    # sets any value outright, including the ones space can't reach - a
    # manual width.
    def open_row(self, key):
        self.error = None
        if key == "hidden":
            self.error = self._set_hidden(self.column.name not in self.settings.hidden)
        elif key == "colored":
            self._set_colored(self.column.name in self.settings.no_color)
        elif key == "sort":
            self._set_sort(self._SORT_CYCLE[self._sort_name()])
        elif key == "width":
            # auto <-> fit; a manual width goes back to auto.
            self._apply_width("fit" if self._width_name() == "auto" else "auto")
        # Anything else is a read-only fact or the error row: re-rendering
        # is still right, since it clears a stale error.
        return True

    def edit_row(self, key):
        self.error = None
        name = self.column.name
        if key == "hidden":
            return RowEdit(f"Hide {name}?", "yes" if name in self.settings.hidden else "no",
                           lambda v: self._set_hidden(v == "yes"), YES_NO)
        if key == "colored":
            return RowEdit(f"Color {name}'s options?", "no" if name in self.settings.no_color else "yes",
                           lambda v: self._set_colored(v == "yes"), YES_NO)
        if key == "sort":
            return RowEdit(f"Sort by {name}", self._sort_name(), self._set_sort, SORT_OPTIONS)
        if key == "width":
            auto = "" if self.auto_width is None else f" ({self.auto_width})"
            fit = "" if self.fit_width is None else f" ({self.fit_width})"
            return RowEdit(
                f"Width for {name}: auto{auto}, fit{fit}, or a number",
                self._width_name(), self._apply_width,
            )
        return None

    def _set_hidden(self, hide: bool):
        name = self.column.name
        if not hide:
            if name in self.settings.hidden:
                self.settings.hidden.remove(name)
        elif name not in self.settings.hidden:
            # Same guard as action_hide_column: a table with every column
            # hidden has no cursor to un-hide them from.
            if self.visible_column_count <= 1:
                return "Cannot hide the last visible column"
            self.settings.hidden.append(name)
        self._save()
        return None

    def _set_colored(self, colored: bool):
        name = self.column.name
        if colored and name in self.settings.no_color:
            self.settings.no_color.remove(name)
        elif not colored and name not in self.settings.no_color:
            self.settings.no_color.append(name)
        self._save()
        return None

    def _width_name(self):
        name = self.column.name
        if name in self.settings.widths:
            return str(self.settings.widths[name])
        return "fit" if name in self.settings.fit else "auto"

    def _apply_width(self, text):
        """The Width on-edit hook: one of three strategies, by what was
        typed. `auto` (or blank) sizes to the average value, capped;
        `fit` to the longest value, uncapped; a number is that width. Each
        replaces the others."""
        text = text.strip().lower()
        name = self.column.name
        if text in ("", "auto", "fit"):
            self.settings.widths.pop(name, None)
            if text == "fit":
                if name not in self.settings.fit:
                    self.settings.fit.append(name)
            elif name in self.settings.fit:
                self.settings.fit.remove(name)
        else:
            try:
                value = int(text)
            except ValueError:
                return f"'{text}' isn't auto, fit, or a number"
            if value < 1:
                return "Width must be at least 1"
            self.settings.widths[name] = value
            if name in self.settings.fit:
                self.settings.fit.remove(name)
        self._save()
        return None

    # Sort is a property of the table (one column at a time), so setting
    # it here also clears it off whichever column held it.
    _SORT_CYCLE = {"none": "ascending", "ascending": "descending", "descending": "none"}

    def _sort_name(self):
        if self.settings.sort_column != self.column.name:
            return "none"
        return "descending" if self.settings.sort_direction == "desc" else "ascending"

    def _set_sort(self, value):
        if value == "none":
            if self.settings.sort_column == self.column.name:
                self.settings.sort_column, self.settings.sort_direction = None, None
        else:
            direction = "desc" if value == "descending" else "asc"
            self.settings.sort_column, self.settings.sort_direction = self.column.name, direction
        self._save()
        return None

    def result(self):
        return self.changed
