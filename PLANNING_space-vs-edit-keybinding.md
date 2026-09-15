# Planning: space = view/open externally, `e` = edit

Status: idea, scoped and reviewed in session, not yet implemented. Captured
2026-08-20. See CLAUDE.md for `action_edit_cell`/`_edit_cell_ctx`/
`ACTION_CONTEXTS` background.

## The idea

Split dbman's overloaded `e` key into two mnemonics: **space = "see it more
truly"** (open externally / view, non-destructive) vs **`e` = edit** (change
it, in-app). Originally proposed with three examples: spacebar opens a URL
field in a browser, spacebar on a Notion row opens it in Notion itself, and
`e` on a Notion row opens the row body in a text editor as Markdown.

## Why `e` needs splitting

Today `e` (`action_edit_cell`, `dbman.py:1816-1907`) is already overloaded in
row-select mode: it calls `action_edit_document` (in-app whole-row edit) when
`Capabilities.whole_row_edit` is set, but falls back to silently opening a
browser (`action_open_in_browser`) when the provider only has
`Capabilities.open_in_browser` (Notion today). `_edit_cell_ctx`'s own
docstring already flags this as one of the app's messiest cross-cutting
branches. There is also **no field/cell-level "open externally" concept at
all** today — `open_in_browser` is a row-only capability, and `Column` has no
URL-type flag.

Space is currently completely unbound at the app level (confirmed via grep of
`dbman.py`) and doesn't collide with any Textual default binding on the
widgets dbman actually uses (`DataTable`, `ListView`, `Screen`, `App` — only
`SelectionList`/`Tree`, which dbman never instantiates, bind `space` in the
installed Textual package). Clean slate.

## Scope decided this session

Two open questions were resolved with the user:

1. **Notion row body as Markdown is explicitly out of scope for this pass.**
   That's the feature CLAUDE.md already documents as deliberately deferred —
   it needs a bidirectional block↔Markdown converter plus a non-atomic
   archive-then-reappend write path, "a project of its own." This plan
   implements the keybinding *split* using capabilities that already exist
   (`Capabilities.open_in_browser`, `get_row_url`). Until an in-app Notion row
   editor exists, `e` on a Notion row will notify the user to press space
   instead of silently opening a browser — no more `e`-opens-a-browser
   fallback.
2. **Field-level URL detection sniffs the cell's string value**
   (`^https?://` prefix) rather than adding new column metadata — provider-
   agnostic, no `Provider`/`Column` ABC changes needed. (The alternative,
   adding a declared `Column.url: bool` flag, was considered and rejected as
   more precise but unnecessary scope.)

## Proposed changes (all in `dbman.py`, no provider changes needed)

**1. New binding** — near the existing `e` binding in `DbMan.BINDINGS`
(~`dbman.py:1103-1140`):
```python
Binding("space", "open_external", "Open"),
```

**2. New action `action_open_external`** — replaces the row-mode fallback
currently embedded in `action_edit_cell`. Forks on `self.select_mode`, guarded
the same way today's `action_open_in_browser` (`dbman.py:1925-1943`) already
guards (`self.mode == "view"`, `isinstance(self.focused, DataTable)`,
`self.current_item` set):
- **row mode**: unchanged logic from today's `action_open_in_browser` —
  requires `provider.capabilities.open_in_browser`, resolves `row_key` from
  the cursor row, calls `provider.get_row_url(...)`, `webbrowser.open(...)`.
  Reuse this body verbatim.
- **field mode**: read the focused cell's current value using the same
  value-read `action_edit_cell`'s field-mode branch already uses to seed
  `EditCellScreen` (reuse that exact code path — don't re-derive a second
  way to read a cell). If the stringified value matches `^https?://`,
  `webbrowser.open()` it; otherwise notify `"No external view for this
  field"` (`severity="error"`).
- **column mode**: notify `"No external view in column mode"`.

**3. Trim `action_edit_cell`'s row-mode branch** (`dbman.py:1840-1847`) — drop
the `elif self.provider.capabilities.open_in_browser` fallback entirely:
```python
if self.select_mode == "row":
    if self.provider.capabilities.whole_row_edit:
        self.action_edit_document()
    else:
        self.notify("No in-app row editor for this provider — press space to open externally", severity="error")
    return
```

**4. Update `_edit_cell_ctx`** (`dbman.py:929-945`) — row-mode branch drops
the `open_in_browser` OR-clause. Use `None` (visible-but-inert) rather than
`False` (hidden) when there's no in-app editor, matching the app's existing
"nothing to do right now" convention (`Z`/`F`'s greyed-out precedent) — keeps
`e` visible as a hint instead of vanishing:
```python
if app.select_mode == "row":
    if app.provider.capabilities.whole_row_edit:
        return True
    return None
```

**5. New `_open_external_ctx` predicate**, registered in `ACTION_CONTEXTS`
(~`dbman.py:1149`) as `"open_external": _open_external_ctx`:
```python
def _open_external_ctx(app):
    if app.mode != "view":
        return False
    if app.select_mode == "column":
        return False
    if app.select_mode == "row":
        return app.provider.capabilities.open_in_browser
    # field mode: deliberately not gated on rows_editable like _edit_cell_ctx —
    # viewing/opening a cell externally doesn't require the cell to be editable.
    return app.current_type in ("table", "view")
```

**6. `ShortcutsScreen`** — add the `space` / "Open Externally" row alongside
`e` / "Edit", per CLAUDE.md's "keep BINDINGS and the shortcuts panel in sync"
convention.

**7. `re` import** — check whether `dbman.py` already imports `re`; add if
needed for the URL match.

## Docs to update alongside implementation

CLAUDE.md narrates the current behavior in detail in two places that would go
stale:
- The `select_mode`/`e` polymorphism paragraph in "UI structure" (describes
  `e`'s row-mode fallback to `action_open_in_browser`).
- The NotionProvider section's "Row editing hands off to Notion itself..."
  paragraph (currently says `e` triggers the browser hand-off; needs to say
  `space` does, and `e` now points there instead of doing it).

## Verification plan (once implemented)

No test suite for this app — verify by running:
- `./dbman garden.sqlite` — SQL table has no `open_in_browser`, so row-mode
  space notifies unsupported; field-mode space on a non-URL cell notifies "no
  external view"; `e` unaffected for field-mode edits.
- Against a real Notion page (CLAUDE.md's "Testing the Notion provider") —
  row-mode `space` opens the Notion page in the browser (replacing what `e`
  used to do); row-mode `e` now notifies pointing at space; field-mode
  `space` on a `url`-type property opens that URL; footer shows `e`
  greyed-but-visible in row mode (not hidden).
- Against local CouchDB (CLAUDE.md's CouchDB testing section) — row-mode `e`
  still opens the whole-document JSON editor unchanged (`whole_row_edit` path
  untouched).

## Follow-on idea: per-column configurable editor/opener

Raised same session, after the core split above. The gap: type-based
inference alone can't always pick the right UI. Two Notion `rich_text`
columns (e.g. a short "name" vs. a long "description") share a `type_name`
but want different edit widgets; and "open externally" shouldn't be hardcoded
to a browser — sometimes the right opener is `$EDITOR` (vim) on the raw text.
The proposal: let a column declare its editor/opener explicitly, with
type-based inference as the default when no override is set.

**Precedence order** when `e` (field mode) or `space` (field mode) fires on a
cell: explicit per-column override → provider/type-based default (e.g.
Notion `type_name == "url"` → opener `browser`) → generic fallback (today's
plan: value-sniffing `^https?://` for opener, `EditCellScreen` single-line
for editor).

**Where the override lives**: alongside the other per-column display prefs
(`view_settings.py`'s `.dbman/<db>.json` — widths, order, hidden), as two new
per-column maps, e.g. `settings.column_editor: dict[str, str]` and
`settings.column_opener: dict[str, str]`. Same store, same persistence
mechanism, no new file.

**Where it's configured**: reuse the dead column-mode slot on `e` —
CLAUDE.md already notes "column mode currently has no `e` action (`t`/`H`/`L`
already act at column granularity)." Column-select-mode `e` opening a new
"configure this column" modal is consistent with `e`'s existing
select-mode-polymorphism (field edits the cell, row edits/opens the row, this
adds column configures the column). Needs `_edit_cell_ctx`'s column branch to
flip from `return False` to something like `return app.current_type in
("table", "view")`, and a new modal (`ColumnBehaviorScreen`?) with two
choices (editor UI, opener), each defaulting to "Auto" plus an explicit list.

**Editor UI choices** — reuse existing modals rather than inventing new ones
where possible:
- "Auto" (today's inference)
- "Single-line" → today's `EditCellScreen`
- "Multi-line" → likely reuses `EditTextScreen` (already a plain textarea
  modal used for SQL/CouchDB view definitions) rather than building a new
  widget

**Opener choices**:
- "Auto" (today's inference: row → `get_row_url`, field → URL-value sniff)
- "Browser" (today's `webbrowser.open`)
- "`$EDITOR`" — **new capability, and dbman's first shell-out to an external
  process.** `ISSUE_DRAFT_attachments-directory.md` flags the same boundary
  ("No existing precedent for shelling out... every edit is an in-app
  Textual modal") for its attachments-mirroring proposal — worth keeping the
  two consistent (e.g. both using Textual's `app.suspend()` context manager
  to hand the terminal to a blocking subprocess) if both ever get built.

**Open questions**:
- Does the type-based default table live per-provider (natural, since
  `type_name` vocab is provider-specific) or does `dbman.py` need a small
  provider-agnostic mapping layer?
- Is this Notion-first (richest type vocabulary — `url`, `title` vs.
  `rich_text`, `select`, etc.) or should SQL/CouchDB get sensible defaults
  too from day one?
- Should "Auto" ever be smarter than "no override set" — e.g. infer
  multi-line for a `rich_text` column whose *sampled values* are long, the
  same spirit as CouchDB's existing sampling-based schema inference?
- Interaction with `add_row`: a new row's fields would presumably use the
  same per-column editor default.

## Non-goals for now

No code changes have been made as part of this planning session. The Notion
row-body-as-Markdown editor (in-app editing of a page's block content) stays
a separate, larger, not-yet-scoped future project. The per-column
editor/opener override above is likewise not scoped for implementation yet —
it's a documented follow-on, not part of the initial space/e split.
