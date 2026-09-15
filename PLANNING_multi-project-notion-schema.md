# Planning: consolidated Features/Bugs across projects (HGF, ADWG, ...)

Status: idea, not yet designed in detail. Captured 2026-08-18 mid-session; not
acted on. See CLAUDE.md for dbman's Notion provider architecture.

## The idea

Instead of each software project having its own separate "Features" and
"Bugs" Notion databases (current state: hgf2 has "Spec"/"Bugs" under the
**HGF - App** page; the liturgical PWA has its own "Features"/"Bugs" under
**A Day With God**), consolidate into **one shared "Features" database and
one shared "Bugs" database** across all projects, distinguished by a
`Project` property (e.g. `HGF`, `ADWG`, ...).

Each project keeps its own Notion page (e.g. "HGF - App"), but instead of
owning its own database, that page shows **views** into the shared
Features/Bugs databases filtered to its `Project`. When `dbman` is run from
a given project's working directory, it should likewise show "Features" and
"Bugs" tables pre-filtered to that project.

Each project would also need its own **Areas** collection — today "Area" is
a flat `select` property on each database with per-project option values
hardcoded into the schema (ADWG's Area options are Angular/PowerShell/
CouchDB/Nginx/Calendar/Docs/Other/pyimport/pyparse). A shared database can't
have one global Area select list that makes sense across unrelated projects,
so this probably needs to become a per-project lookup (relation to a
per-project "Areas" database, or at minimum a differently-scoped property).

Rough sequencing guess: extend the **ADWG** Features/Bugs databases (since
they already have `ID` (auto_increment_id) columns with prefixes `FEAT`/
`BUG` established — see prefix collision note below) rather than starting
fresh, and import HGF's Spec/Bugs rows into them.

## Current state (as of this session, both fetched directly from Notion)

**ADWG "Features"** (`collection://9dca8953-e78f-408f-a2e9-03a38a3d3e60`,
under page `78a27682-f926-40c5-a14f-7eaf080f087b`, itself under "A Day With
God" `3a502ac1-58c9-81fc-8335-f31bceecafcd`):
`Feature`(title), `Description`(text), `Type`(select: Feature Request/Audit
Finding), `Priority`(select: P0-P3), `Status`(select: New/Needs Human
Review/Reviewed - Ready for AI/Queued for Build/In Progress/Needs Fix/
Shipped/Not Planned), `Area`(select: Angular/PowerShell/CouchDB/Nginx/
Calendar/Docs/Other/pyimport/pyparse), `Submitted By`(text), `GitHub
Issue`(url), `PR`(url), `Review Notes`(text), `ID`(auto_increment_id, prefix
`FEAT`).

**ADWG "Bugs"** (`collection://6f043417-5ee4-4aae-9a66-b05bc45bf56e`, under
page `e78cc1b1-ecff-4268-a72e-f48b5396e554`): `Bug`(title), `Area`(select,
same options minus pyimport), `Severity`(select: P0-P3), `Status`(select:
New/Needs Human Review/Reviewed - Ready for AI/Queued for Build/In Progress/
Fixed/Not Planned), `Submitted By`(text), `GitHub Issue`(url), `PR`(url),
`Review Notes`(text), `ID`(auto_increment_id, prefix `BUG`).

**HGF "Spec"** (`collection://f17e9c8c-454a-4415-b90e-2242d8a58f39`, under
"HGF - App" `38302ac1-58c9-81d7-aa52-fa831956e7fa`): `Feature`, `Notes`,
`PR`, `Platform`, `Review Notes`, `Section`, `Severity`, `Status`, `Type`.
No `Area` — has `Section`/`Platform` instead.

**HGF "Bugs"** (`collection://01da2631-04d4-43b9-832b-1d126136143f`):
`Bug`, `Area`, `PR`, `Remedy`, `Review Notes`, `Severity`, `Status`. Has a
`Remedy` field ADWG's Bugs doesn't.

## Prefix collision hit this session

Tried adding a `UNIQUE_ID PREFIX 'FEAT'` column to HGF's Spec and `PREFIX
'BUG'` to HGF's Bugs (to get `FEAT13`/`BUG3`-style references) — both failed
with `409 conflict_error: Unique ID prefix is already in use`, because
Notion enforces `unique_id` prefixes workspace-wide and ADWG's databases
already claim `FEAT`/`BUG`. This is what surfaced the "these should probably
be one set of tables" idea in the first place — no changes were made to
either project's schema as a result.

## Open design questions for later

- **Schema reconciliation**: ADWG's `Priority` (Features) vs HGF's
  `Severity` naming; HGF's `Section`/`Platform`/`Remedy` fields have no ADWG
  equivalent yet — decide which become shared columns vs stay project-
  specific (Notion doesn't support per-view-only columns, so "project-
  specific field" either means a shared column that's blank for other
  projects, or living with the merge being lossy/lossless-with-nulls).
- **Area**: flat `select` won't scale across projects with unrelated area
  vocab. Likely needs to become a `relation` to a project-scoped "Areas"
  database, or a compound value. Unclear whether Notion relations play well
  with dbman's `_EDITABLE_TYPES` (currently relation is display-only, see
  `providers/notion_provider.py`).
- **Where do the shared databases live?** A parent/hub page, with each
  project page (HGF - App, A Day With God) holding a *linked database view*
  filtered to its `Project`? Notion linked views have their own view ID
  distinct from the source database ID.
- **dbman support gap — this is the real blocker.** `NotionProvider`
  currently (see `providers/notion_provider.py` docstring) only discovers
  databases *owned* by the connected page's block tree, explicitly ignoring
  "a linked database view whose true parent page is different." It also has
  no filter/view concept at all — `list_views()` always returns `[]`. To get
  "run dbman in this directory, see Features/Bugs filtered to HGF" dbman
  itself needs new capability: either (a) teach it to discover and query
  linked-view blocks with their baked-in filter, or (b) add filter
  configuration to the `notion://` connection URL / `dbman.json` session so
  a connection can point at a shared database ID plus a fixed `Project =
  X` filter applied on every query. (b) is probably less Notion-API-fragile
  since it doesn't depend on how Notion represents linked views internally.
- **Migration mechanics**: importing HGF's ~9-column Spec rows and Bugs rows
  into the (slightly different-schema) ADWG databases — one-time script vs.
  manual, and whether `Project` needs backfilling on ADWG's *existing* rows
  too (they'd all become `Project = ADWG`).

## Non-goals for now

No changes have been made to any Notion schema or data as part of this
plan — HGF's Spec/Bugs and ADWG's Features/Bugs are both untouched. hgf2's
`dbman.json` still points `hgf-app` at the HGF - App page with Spec/Bugs
discovered as separate, unrenamed tables.
