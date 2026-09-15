<!--
Draft GitHub issue, not yet filed — GitHub issue creation failed with a 404
(looks like the MCP GitHub token lacks issues:write). Paste the sections
below into a new issue on krewmarco/dbman, then delete this file.
-->

# Title

Mirror CouchDB _attachments as a filesystem directory for native-editor round-tripping (extends #5)

# Labels

enhancement, priority: P2, area: attachments

# Body

## Summary

#5 proposes browsing individual attachments (open HTML in a browser, text in `$EDITOR`, binary as save-to-disk). This issue proposes a stronger mechanism that subsumes that: hitting `e` on a document's `_attachments` field mirrors *all* of that document's attachments onto the real filesystem as a directory, opens the directory in the OS file manager, and — on an explicit second trigger — reconstructs the `_attachments` field from whatever's on disk. The user edits with whatever native app they want per file (browser for HTML, image editor for a photo, `$EDITOR` for text, etc.) instead of dbman picking one opener per content-type.

Recommend keeping #5 open until this design is accepted, then closing/consolidating — same sequencing #7 used against #4.

## Why this is easier than it sounds

CouchDB's own data model does most of the work:

- **Attachment keys are already filenames.** A doc's `_attachments` map is keyed by name (`main.html`, `photo.jpg`, ...), almost always with a real, meaningful extension already. Mirroring to disk is just "write each attachment's bytes to a file named after its key" — no bespoke extension convention needs inventing for existing attachments.
- **Content-type only needs inferring for genuinely new files.** Python's `mimetypes.guess_type()` on the new file's extension, falling back to `application/octet-stream`, covers the one case (a file added to the directory that wasn't an attachment before) where dbman has to guess anything.
- **CouchDB supports stub carryover on doc PUT.** A document PUT's `_attachments` map can mix untouched attachments as stubs (`{"content_type":..., "stub": true}`, referencing the existing digest — no re-upload) with changed/new ones inlined as base64 (`{"content_type":..., "data": "..."}`). This means the whole directory's changes (added, modified, deleted files) reconstruct via **one atomic PUT with one rev bump**, not N sequential single-attachment REST calls each needing its own rev.

## Proposal

**1. Prerequisite fix — `_attachments` isn't reliably reachable today.** `CouchDBProvider._infer_columns` (`providers/couchdb_provider.py:118-139`) ranks the top `MAX_INFERRED_COLUMNS = 15` keys by frequency across a 200-doc sample. A field that only exists on documents that actually have attachments can get pushed out of that window entirely, leaving no column/cursor to invoke `e` on. Fix: special-case `_attachments` into the inferred column set whenever present on any sampled doc, bypassing the frequency ranking (same spirit as `_id`/`_rev`'s existing priority pinning).

**2. New capability + provider methods**, gated the same way every other CouchDB-only feature is:
- `Capabilities.attachments: bool` (default `False`; `True` only for `CouchDBProvider`) — the flag #5 already proposed.
- `Provider.get_attachment(row_key, filename) -> bytes` — `GET /db/docid/attname`, CouchDB streams the right `Content-Type` directly. (Also #5's proposal — `self.raw_docs[row_id_str]` already holds the stub *metadata*, content_type/length/digest/revpos, from the existing `_find` fetch; only the bytes are missing today.)
- `Provider.sync_attachments(row_key, files: dict[str, bytes]) -> RowKey` — re-`GET`s the doc fresh (same re-GET-before-PUT pattern already used in `update_cell`/`update_row_json`), diffs its current `_attachments` stubs against `files`: unchanged (local file's md5 matches the stub's `"md5-..."` digest) → keep as stub; changed or new → inline as base64; present in the old doc but missing from `files` → dropped from the map (deleted). Single PUT.

**3. New `dbman.py` orchestration**, dispatched from `action_edit_cell` when `column_name == "_attachments"` (a new branch ahead of the existing dict/list auto-route at `dbman.py:1699-1701`, which today would otherwise just open the whole document as JSON):
- **Mirror out**: fetch each attachment via `get_attachment`, write to `.dbman/<db_name>/attachments/<doc_id>/<filename>`, `open`/`xdg-open` the directory.
- **Mirror back**: pressing `e` on the same field again (see "commit signal" below) reads the directory's current contents and calls `sync_attachments`.

Directory location follows the existing local-state convention (`view_settings.py:32-39`'s `.dbman/<db_name>.json`) rather than inventing a new top-level `./dbman/` path — `.dbman/` is already gitignored, so the mirror inherits that for free.

## Open design question: how does dbman know the user is "done"?

There's no OS-level signal for "the user finished editing a folder in Finder" — GUI editors/file managers are almost all non-blocking, so a naive "open it and wait" doesn't work. Three options considered:

- **Explicit re-trigger (recommended)**: pressing `e` on `_attachments` again while a mirror is open is the commit signal. Consistent with this codebase's existing no-magic, explicit-action posture — nothing today auto-saves on blur or watches the filesystem. Also means "commit" always re-fetches the doc fresh, which is naturally robust against the document having changed concurrently while the user was off editing files (same non-transactional risk posture CLAUDE.md already documents as accepted elsewhere, e.g. CouchDB's re-GET-before-PUT update pattern).
- **Filesystem watcher** (inotify/FSEvents): needs a new dependency and a background task running alongside Textual's event loop; "done" is still fuzzy since some editors autosave on every keystroke.
- **Blocking `$EDITOR` subprocess**: works for #5's narrower single-attachment case, but breaks down here — a directory of heterogeneous files meant for different native apps has no single process to block on.

## Secondary open questions (not blockers, but worth deciding before implementation)

- **Large attachments**: base64-inlining in one doc PUT costs ~33% size overhead and counts against CouchDB's configured max document size. Fine for typical text/HTML/small-image attachments; a genuinely large binary attachment might eventually need the standalone `PUT /db/doc/attname?rev=X` streaming path instead of the single-PUT approach above. Proposal: punt for v1, revisit only if it's actually hit.
- **Cross-platform "open directory"**: `open` (macOS) / `xdg-open` (Linux) dispatch is trivial to add; this codebase doesn't target Windows anywhere else, so no need to build for it here either.
- **No existing precedent for shelling out.** Nothing in dbman today calls `subprocess`/`Popen`/opens `$EDITOR`/a browser — every edit is an in-app Textual modal. This feature (and #5) would be the first departure from that; worth being deliberate about the boundary (dbman spawns the file-manager open and then gets out of the way, rather than trying to manage/track the user's editor process).

## Depends on / relates to

- Extends/likely-supersedes #5 (attachment browsing) — recommend closing #5 once this design is accepted.
- Uses the `.dbman/` local-state convention established for #1's per-view settings file.
