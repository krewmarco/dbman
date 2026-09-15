# Opinion: fully in-memory "virtual tables" with change tracking + periodic auto-sync

Status: opinion given during a design discussion, not pursued further (the
user was just exploring the idea). Captured 2026-08-22. Mirrored to the
"Docs" database on the dbman Notion page.

## The question

Could dbman tables operate entirely in memory, decoupled from their backend
provider, with local change tracking that periodically auto-syncs to the
remote provider — motivated by high-latency connections (in practice this
mostly means Notion; SQLite is local and CouchDB is presumably LAN)?

## Grounding facts (from the codebase)

- No such concept exists today. The only "virtual" hit in the repo is
  Python's *virtualenv* — nothing dbman-level.
- dbman never holds more than one page in memory. `load_item()` rebuilds
  `self.row_keys`/`row_values`/`row_order`/`rendered_rows` from scratch on
  every navigation, sourced from one `provider.get_page(..., page_size=500)`
  call. `RowPage.has_more`/`next_cursor` assume paged, on-demand fetching —
  "hold the whole table in memory" conflicts with this directly.
- Every write today is a synchronous, single round-trip, honestly
  non-transactional action. SQL does `UPDATE ... WHERE rowid=:rid`; CouchDB
  re-GETs immediately before PUT (can still 409); Notion has no revisioning
  at all. CLAUDE.md is explicit that this is a deliberate, accepted risk
  posture in exchange for simplicity — not an oversight.
- The closest existing precedent is row reordering (`action_move_row`/
  `_sync_row_order` in dbman.py, `NotionProvider.reorder_rows`): move
  locally with zero network calls, debounce, diff against a baseline to find
  the touched slice, sync once via a background thread, reconcile with a
  full reload. But it's deliberately narrow — one field, one burst,
  sub-second debounce — and on failure it just notifies and gives up: local
  state is left diverged from the backend until the next full reload.

## Opinion

Split into two different problems:

**Reads** (prefetching a table into memory so browsing feels instant despite
latency): plausible, moderate effort. Background-fill pages via the existing
`has_more`/`next_cursor` cursor, using the same worker-thread pattern already
proven for reorder sync, into a local per-table cache. Bounded and additive
— doesn't change write semantics, worst-case failure mode is a stale read.
Should be opt-in and visibly labeled (`[CACHED]` / "last synced Ns ago"),
not silent — dbman's whole model today is "always live, reload to see
external changes."

**Writes** (queue edits locally, dirty-track, auto-sync on a timer): pushing
back on this as a general feature.
1. dbman's entire concurrency story today is "what you see the moment you
   press Enter is what actually happened, or a visible error." A write
   buffer breaks that contract for however long the sync interval is, across
   three backends with three different, already-thin conflict models.
   Reconciling that honestly needs a dirty/pending indicator per cell, a
   conflict-resolution UI, and a retry/requeue path — none of which the
   reorder precedent has; it just gives up and notifies on failure, which is
   fine for "did my row move," not for "did my edit survive."
2. "Periodically" (timer-driven, independent of user action) is materially
   different from the reorder pattern's "debounced after a burst of
   activity." A background timer syncing regardless of whether the user is
   still editing invites surprise conflicts dbman has so far avoided by
   keeping every write a deliberate, synchronous, user-triggered action.
3. The actual pain point — Notion write latency — doesn't need a general
   decoupled-storage abstraction. It needs batching *within a deliberate
   user action*: e.g. multiple cell edits within one row-edit session
   batched into a single multi-property `PATCH`, still synchronous and
   still capability-gated.

**Bottom line:** wouldn't build a generic in-memory "virtual table" layer
with a background auto-sync timer — real architecture/UX commitment
(conflict handling, staleness indicators, retry semantics) for a project
that has so far deliberately traded that complexity away everywhere. Build
the read-side prefetch/cache if latency during *browsing* is the actual
complaint; extend the existing per-burst debounce+batch pattern (scoped to
explicit user actions, not a timer) if latency during *editing* is the
complaint.

## If revisited later

Two independent, much smaller follow-ups:
- **Table prefetch/cache**: background-load full pages into a per-connection
  in-memory store using the worker-thread + `call_from_thread` pattern from
  `_sync_row_order`; add a capability flag; visible staleness indicator;
  decide eviction/refresh policy.
- **Batched multi-cell edit sync**: extend the debounce pattern from
  `action_move_row` to cell edits within a single row-edit session, still
  synchronous/user-triggered rather than timer-driven; scope conflict
  handling explicitly rather than assuming reload-and-hope.
