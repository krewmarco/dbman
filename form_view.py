"""Form mode: one row of dbman's table at a time, every field in full.

The grid truncates long values to fit a column; this shows the record the
grid's cursor is on as a page of labelled fields, each value whole and
wrapped, with the plain text fields editable in place. It holds no rows of
its own: the DataTable stays loaded behind it (same hidden columns, order,
filters and sort), and moving to the next record moves that table's cursor.
So the grid's settings are the form's settings, and leaving form mode puts
the grid on the record you were reading.

The app owns the data; this widget only renders a `FormField` list and asks
the app (`form_commit`, `form_step`, `form_edit_other`) to do anything that
touches a provider.
"""

from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static, TextArea


@dataclass
class FormField:
    name: str
    label: str          # header text, with dbman's key/computed marks
    value: Any          # untruncated, as the row holds it
    text: str           # what's shown (and seeds the editor)
    editable: bool      # edited in place, as text
    note: str = ""      # why it isn't editable, shown dim next to the label


class FieldEditor(TextArea):
    """A field's in-place editor. Leaving it (escape, tab, or moving to the
    next record) saves; ctrl+n/ctrl+p save and step records without leaving
    the field, which is the loop the form exists for: read, write, next."""

    BINDINGS = [
        Binding("escape,tab", "done", "Done", show=False),
        Binding("ctrl+n", "record(1)", "Save & Next"),
        Binding("ctrl+p", "record(-1)", "Save & Prev"),
    ]

    def __init__(self, field_name: str, text: str, **kwargs):
        super().__init__(text, soft_wrap=True, tab_behavior="focus", **kwargs)
        self.field_name = field_name
        self.original = text

    def action_done(self) -> None:
        self.form.focus()

    def action_record(self, delta: int) -> None:
        self.form.step(delta, keep_editing=True)

    def on_blur(self) -> None:
        self.form.commit(self)

    @property
    def form(self) -> "FormView":
        return next(a for a in self.ancestors if isinstance(a, FormView))


class FormView(VerticalScroll, can_focus=True):
    BINDINGS = [
        Binding("j,down", "field(1)", "Next Field", show=False),
        Binding("k,up", "field(-1)", "Prev Field", show=False),
        Binding("g", "field_edge(0)", "First Field", show=False),
        Binding("G", "field_edge(1)", "Last Field", show=False),
        Binding("J", "record(1)", "Next Record"),
        Binding("K", "record(-1)", "Prev Record"),
        Binding("e,enter", "edit", "Edit Field"),
        Binding("space", "open", "Open"),
    ]

    DEFAULT_CSS = """
    FormView {
        height: 1fr;
        padding: 0 1;
    }
    FormView:focus {
        border: double $accent;
    }
    FormView .form-field {
        height: auto;
        padding: 0 1;
        border-left: blank;
    }
    FormView .form-field.-current {
        border-left: thick $accent;
        background: $boost;
    }
    FormView .form-label {
        text-style: bold;
        color: $primary;
    }
    FormView .form-value {
        height: auto;
    }
    /* Reads like the read-only fields until you edit it. */
    FormView FieldEditor {
        height: auto;
        max-height: 16;
        border: none;
        padding: 0;
        background: transparent;
    }
    FormView FieldEditor:focus {
        border: tall $accent;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fields: list[FormField] = []
        self.current = 0
        self._signature = None
        self._boxes: list[Vertical] = []
        self._values: list[Static | FieldEditor] = []

    # -- rendering ---------------------------------------------------------

    def show(self, fields: list[FormField], edit_current: bool = False) -> None:
        """Render a record. The widgets are built once per column layout and
        refilled for each record, so stepping records is just new text."""
        self.fields = fields
        signature = tuple((f.name, f.label, f.editable, f.note) for f in fields)
        fresh = signature != self._signature
        if fresh:
            self._signature = signature
            self.remove_children()
            self._boxes, self._values = [], []
            for f in fields:
                label = Text(f.label, style="bold")
                if f.note:
                    label.append(f"  {f.note}", style="dim italic")
                value = FieldEditor(f.name, f.text) if f.editable else Static(classes="form-value")
                box = Vertical(Static(label, classes="form-label"), value, classes="form-field")
                self._boxes.append(box)
                self._values.append(value)
            self.mount_all(self._boxes)
        for f, value in zip(fields, self._values):
            if isinstance(value, FieldEditor):
                if fresh:
                    continue  # built with this text
                value.original = f.text
                value.load_text(f.text)
            else:
                value.update(Text(f.text) if f.text else Text("(empty)", style="dim"))
        self.current = min(self.current, max(len(fields) - 1, 0))
        self._mark_current()
        if edit_current:
            self.call_after_refresh(self.action_edit)

    def _mark_current(self, scroll: bool = True) -> None:
        for i, box in enumerate(self._boxes):
            box.set_class(i == self.current, "-current")
        self.refresh_bindings()  # whether space opens depends on the field
        if scroll and self._boxes:
            self.call_after_refresh(self.scroll_to_widget, self._boxes[self.current], animate=False)

    # -- actions -----------------------------------------------------------

    def action_field(self, delta: int) -> None:
        if self.fields:
            self.current = max(0, min(len(self.fields) - 1, self.current + delta))
            self._mark_current()

    def action_field_edge(self, end: int) -> None:
        if self.fields:
            self.current = len(self.fields) - 1 if end else 0
            self._mark_current()

    def action_record(self, delta: int) -> None:
        self.step(delta, keep_editing=False)

    def action_edit(self) -> None:
        if not self.fields:
            return
        value = self._values[self.current]
        if isinstance(value, FieldEditor):
            value.focus()
            value.move_cursor(value.document.end)
        else:
            self.app.form_edit_other(self.fields[self.current])

    def action_open(self) -> None:
        if self.fields:
            self.app.form_open(self.fields[self.current])

    def check_action(self, action: str, parameters) -> bool | None:
        if action == "open":
            # Greyed (None), not hidden, when this field has nothing to open.
            return True if self.fields and self.app.form_can_open(self.fields[self.current]) else None
        return True

    # The app's j/k/g/G bindings call these on whatever has focus.
    def action_cursor_down(self) -> None: self.action_field(1)
    def action_cursor_up(self) -> None: self.action_field(-1)
    def action_scroll_home(self) -> None: self.action_field_edge(0)
    def action_scroll_end(self) -> None: self.action_field_edge(1)

    # -- app hooks ---------------------------------------------------------

    def commit(self, editor: FieldEditor) -> None:
        if editor.text == editor.original:
            return
        if self.app.form_commit(editor.field_name, editor.text):
            editor.original = editor.text

    def step(self, delta: int, keep_editing: bool) -> None:
        focused = self.app.focused
        if isinstance(focused, FieldEditor):
            # Save before the record changes under the editor; a refused
            # save keeps you on this record with your text intact.
            self.commit(focused)
            if focused.text != focused.original:
                return
            self.current = self._values.index(focused)
        if not keep_editing and isinstance(focused, FieldEditor):
            self.focus()
        self.app.form_step(delta, edit_current=keep_editing)
