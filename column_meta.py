"""The column-metadata virtual table: `e` on a selected column.

Completes the select-mode vocabulary. `e` already means "edit the selected
thing" - field mode edits the cell, row mode edits the row (or hands it to
the provider's own app); column mode was the one slot with nothing behind
it, so every column-level property had to claim a top-level key of its own
(`z` hide, `w` width, `o` sort). That namespace is finite and was filling
up; here, adding a property is adding a row.

Distinct from editing the *table's* schema, which is what Schema mode (`m`)
shows - these are dbman's own display settings for one column, persisted to
.dbman/<db>.json via ViewSettingsStore, plus the provider facts that explain
why some of them are there at all.

See PLANNING_space-vs-edit-keybinding.md, whose follow-on section proposed
exactly this slot for per-column configuration.
"""
from rich.text import Text

from providers.base import Column
from virtual_table import Prompt, VirtualRow, VirtualTable

YES = Text("yes", style="green")
NO = Text("no", style="grey62")
AUTO = Text("auto", style="italic grey62")
NONE = Text("—", style="grey62")


class ColumnMetadataTable(VirtualTable):
    """Rows are one column's properties; the opener does whatever that
    property needs - toggle a flag, cycle a sort, prompt for a width.

    Writes straight through to the ViewSettingsStore on each change, rather
    than batching until save, so it behaves like the single-key actions it
    supersedes (`z`/`w`/`o` each persist immediately). result() reports
    whether anything changed so the caller knows to re-render the table.
    """

    hint = "space or enter edits the selected property · escape closes"
    commits_immediately = True

    # Rows the provider owns: facts about the column, not preferences.
    # Shown because they're the context for the editable rows below (a
    # Colored row only makes sense once you can see the column has options)
    # and because a read-only column is worth knowing before you try to edit.
    def __init__(self, column: Column, item_name: str, view_settings_store, visible_column_count: int):
        self.column = column
        self.item_name = item_name
        self.store = view_settings_store
        self.settings = view_settings_store.get(item_name)
        self.visible_column_count = visible_column_count
        self.title = f"Column: {column.name}"
        self.changed = False
        self.error = None

    def columns(self) -> list[Column]:
        return [Column("Property", "text"), Column("Value", "text")]

    def _sort_value(self):
        if self.settings.sort_column != self.column.name:
            return NONE
        return Text("ascending" if self.settings.sort_direction != "desc" else "descending")

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
        width = self.settings.widths.get(col.name)
        rows.append(VirtualRow("width", [Text("Width"), AUTO if width is None else Text(str(width))]))
        rows.append(VirtualRow("sort", [Text("Sort"), self._sort_value()]))
        if self.error:
            rows.append(VirtualRow(None, [Text("!", style="red"), Text(self.error, style="red")]))
        return rows

    def _save(self):
        self.store.save(self.item_name, self.settings)
        self.changed = True

    def open_row(self, key):
        self.error = None
        if key is None:
            return True  # clears a stale error row without doing anything else
        if key == "hidden":
            return self._toggle_hidden()
        if key == "colored":
            name = self.column.name
            if name in self.settings.no_color:
                self.settings.no_color.remove(name)
            else:
                self.settings.no_color.append(name)
            self._save()
            return True
        if key == "width":
            current = self.settings.widths.get(self.column.name)
            return Prompt(
                f"Width for {self.column.name} (blank for auto)",
                "" if current is None else str(current),
                self._apply_width,
            )
        if key == "sort":
            return self._cycle_sort()
        return False

    def _toggle_hidden(self):
        name = self.column.name
        if name in self.settings.hidden:
            self.settings.hidden.remove(name)
        else:
            # Same guard as action_hide_column: a table with every column
            # hidden has no cursor to un-hide them from.
            if self.visible_column_count <= 1:
                self.error = "Cannot hide the last visible column"
                return True
            self.settings.hidden.append(name)
        self._save()
        return True

    def _apply_width(self, text):
        text = text.strip()
        if not text:
            self.settings.widths.pop(self.column.name, None)
        else:
            try:
                value = int(text)
            except ValueError:
                self.error = f"'{text}' is not a number"
                return
            if value < 1:
                self.error = "Width must be at least 1"
                return
            self.settings.widths[self.column.name] = value
        self._save()

    def _cycle_sort(self):
        """asc -> desc -> none, matching 'o'. Sort is a property of the
        table (one column at a time), so picking it here also clears it off
        whichever column held it."""
        if self.settings.sort_column != self.column.name:
            self.settings.sort_column, self.settings.sort_direction = self.column.name, "asc"
        elif self.settings.sort_direction != "desc":
            self.settings.sort_direction = "desc"
        else:
            self.settings.sort_column, self.settings.sort_direction = None, None
        self._save()
        return True

    def result(self):
        return self.changed
