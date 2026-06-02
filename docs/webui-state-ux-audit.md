# Hermes WebUI state-loss UX audit

Status: architecture / UX audit for Kanban task `t_54059fdf`.
Scope: reload, browser back/forward cache, session switch, WebUI restart/update, reconnect, and multi-tab behavior.

This audit is based on repository contracts plus source inspection of `static/*.js`, `api/*.py`, and state-focused tests in `tests/`. A read-only Claude Code audit was also run as a second pass; its claims were verified before inclusion. One important correction from that lane: activity/tool disclosure state is already persisted through `localStorage` in the current tree, so it is not treated as an unimplemented persistence gap here.

## Executive summary

WebUI already has a strong durable foundation for conversation truth: session sidecars/state-db reconciliation, pending-turn metadata, and the run journal. The highest-risk state-loss UX issues now sit at the boundary between durable runtime truth and browser-only presentation state.

Main findings:

1. Conversation/runtime truth is mostly server-backed. `/api/session` loads session records, reconciles state-db messages, clears stale stream fields, and exposes active stream/pending turn metadata. The run journal records SSE events and supports cursor replay through `after_seq`.
2. Browser reload recovery exists for active streams through `localStorage` snapshots (`hermes-webui-inflight-state`) and journal replay, but it is a recovery cache, not authoritative runtime truth.
3. Several user-visible presentation states still live only in memory/DOM: message scroll position, unsent attached files, voice recording progress, and workspace-panel active tab. These are the clearest sources of “WebUI forgot where I was.”
4. Several `localStorage` maps are shared across tabs but written as last-write-wins without version/merge semantics: inflight snapshots, model preference state, and viewed/unread counters. This creates split-brain or silent clobber risks under multi-tab use.
5. Session identity handling is better than older WebUI patterns: URL `/session/<id>` is preferred over `localStorage`, and `localStorage` is no longer used as a global active-session bus. Remaining risks are around lineage/canonicalization drift and making sure all entry points use the same requested-ID semantics.
6. Queue state intentionally remains browser-side today. That matches the current RuntimeAdapter RFC, but its `sessionStorage` lifetime should be stated as a UX trade-off because it survives reload, not tab close.

Recommended first stabilization slice: persist per-tab message scroll and workspace tab state, then finish draft file restore. These are low-blast-radius UX fixes that do not change runtime ownership.

## Contract alignment

Authoritative docs already define the target model:

- `docs/rfcs/webui-run-state-consistency-contract.md` defines layers: visible transcript, model context, pending turn metadata, live SSE, run journal/replay, compression handoff, live UI scene/cache, and sidebar metadata. It explicitly says SSE is an observation path, not the only durable truth, and replay must be cursor-safe/idempotent.
- `docs/rfcs/canonical-session-resolution.md` says route/query/localStorage/sidebar/direct open/boot restore all provide requested session IDs, and `localStorage` is advisory. Snapshot parents should defer to visible continuation tips for normal chat navigation, while explicit archive browsing remains possible.
- `docs/rfcs/hermes-run-adapter-contract.md` says WebUI may keep presentation state such as expanded rows, selected tabs, and scroll position, but must not privately mutate runtime truth for controls. Runtime ownership should move toward journal/replay/adapter-backed APIs, not process-local `STREAMS` as authority.

This audit follows those contracts: browser state may optimize and preserve the scene, but runtime truth must be server/journal/adapter-backed or clearly marked as transient UX.

## State-surface matrix

| Surface | Current owner / medium | Source-of-truth expectation | Reload risk | Session-switch risk | Update/reconnect risk | Multi-tab risk | UX symptom | Relevant files/tests | Recommended card |
|---|---|---|---|---|---|---|---|---|---|
| Visible transcript | Session sidecar plus state-db merge from `/api/session` | Durable session transcript, replay can rebuild live events without duplicates | Low | Low | Low/medium during interrupted stream | Low | Missing/duplicated messages if reconciliation or replay fails | `api/routes.py:4700-4817`, `docs/rfcs/webui-run-state-consistency-contract.md`, many session tests | Keep covered by replay/reconciliation tests |
| Model context | Backend session/context assembly | Must match visible transcript unless divergence is deliberate and visible | Low | Low | Medium around compression/recovery | Low | Agent responds to context user cannot see | `docs/rfcs/webui-run-state-consistency-contract.md`, `api/streaming.py` | Contract test for visible transcript vs context after recovery |
| Pending turn metadata | `Session.active_stream_id`, `pending_user_message`, `pending_attachments`, `pending_started_at` | Durable bridge for submitted-but-not-finalized turn | Low | Medium when switching while busy | Medium if worker interrupted | Low | Active user turn disappears or duplicates after reload | `api/models.py`, `api/routes.py:4733-4737`, `static/sessions.js:727-785`, `tests/test_stale_stream_*` | Add end-to-end active reload/restart matrix |
| Live SSE stream | `StreamChannel`, `STREAMS`, EventSource; journal writer alongside it | Observation path only; durable truth is journal/session | Medium, mitigated by reattach | Medium, current code closes/reopens transports | Medium on server restart; journal should show stale/interrupted | Medium if multiple tabs observe same stream | Thinking/tool/progress cards flatten or vanish temporarily | `api/config.py:4635-4700`, `api/streaming.py`, `static/messages.js`, `tests/test_inflight_stream_reuse.py` | Runtime replay QA gate |
| Run journal/replay | `_run_journal/{session}/{run}.jsonl`, `RunJournalWriter`, `read_run_events(after_seq=...)` | Cursor-safe, idempotent replay of emitted runtime events | Low | Low | Low for emitted events; active worker can still be gone | Shared durable state | Replay duplication or stale interrupted diagnostics if mishandled | `api/run_journal.py:146-207`, `api/runtime_adapter.py`, `tests/test_run_journal_streaming_static.py` | Keep as runtime backbone |
| Browser inflight snapshot | `localStorage hermes-webui-inflight-state` | Browser recovery cache only, not runtime authority | Medium, bounded TTL and size | Medium | Medium | Medium: shared `localStorage` key, last writer wins | Wrong partial assistant/tool card restored, or lost cache | `static/ui.js:4661-4788`, `static/messages.js:796-805`, `tests/test_1466_bfcache_inflight_reattach.py` | Add per-tab/run timestamp/version checks |
| Active session identity | URL `/session/<sid>` plus `localStorage hermes-webui-session` | URL/requested ID is authoritative for a tab; `localStorage` is advisory boot fallback | Low | Low | Low | Low by design: storage event does not switch other tabs | Wrong session opens if lineage resolver diverges | `static/boot.js:1887-1951`, `static/sessions.js:546-547,724-725,4966-4988`, `tests/test_session_cross_tab_sync.py` | Central canonical resolver coverage |
| Canonical lineage / compression tip | Sidebar lineage helpers and RFC-guided session resolution | Normal navigation opens visible continuation tip; archive mode can inspect snapshots | Medium | Medium | Low | Low | Reload opens archived snapshot or duplicate lineage rows | `static/sessions.js:3425-3456`, `docs/rfcs/canonical-session-resolution.md` | Canonical resolver card before more route work |
| Composer draft text | Server `composer_draft.text` via `/api/session/draft` | Durable per-session draft text | Low | Low | Low | Medium: shared session draft can be overwritten by another tab | Draft text reverts if race with delayed save | `static/sessions.js:50-109`, `api/routes.py:6288-6352`, `tests/test_stage326_composer_draft_validation.py` | Add save-version or last-edit timestamp if multi-tab complaints recur |
| Composer draft files / unsent attachments | `S.pendingFiles`; draft API accepts `files`, restore skips files | Should either restore attachments or explicitly warn they are volatile | High | Medium | High | Per-tab only | Attached files vanish on reload before send | `static/ui.js:1`, `static/sessions.js:50-109`, `static/boot.js:1190-1194` | Restore draft files / attachment metadata |
| Queued follow-up messages | `SESSION_QUEUES` plus `sessionStorage hermes-queue-<sid>` | Browser-side queued intent; not durable runtime truth | Survives reload | Medium | Medium | Per-tab only; dies on tab close | Queued text survives refresh but not closed tab; may surprise users | `static/ui.js:122-147`, `static/sessions.js:826-853`, `tests/test_queue_switch_restore.py` | Document lifetime; consider explicit “tab queue” UI copy |
| Message scroll position | In-memory `_captureMessageScrollSnapshot()` only | Presentation state; per-tab persistence is allowed | High | Medium | High | Per-tab desired | Reload/session switch snaps to bottom or loses reading position | `static/ui.js:6543-6557`; no persistent key found | First stabilization card: per-tab scroll restore |
| Sidebar viewed/unread counts | `localStorage` maps cached in JS variables | Presentation/read-state cache; merge-safe across tabs | Low | Low | Low | Medium: cached read-modify-write can clobber | Unread dot disappears or reappears incorrectly | `static/sessions.js:121-278`, `tests/test_issue856_active_session_read_state.py` | Add merge-on-write/storage listener |
| Sidebar collapse/search/source filter | `localStorage` for collapse/filter; search is intentionally cleared | Presentation prefs | Low | Low | Low | Shared pref is OK | Sidebar chrome differs after bfcache if not synced | `static/boot.js`, `static/sessions.js`, `tests/test_sidebar_collapse_toggle.py`, `tests/test_session_search_bfcache_822.py` | No urgent card |
| Workspace panel open/width | `localStorage` prefs | Presentation state; local browser preference | Low | Low | Low | Shared pref accepted | Right panel flashes/closes if restore order breaks | `static/boot.js:1409-1430,1934-1961`, `tests/test_sprint37.py` | Keep tests |
| Workspace panel active tab | `_workspacePanelActiveTab = 'files'` in memory | Presentation state; should persist per browser/workspace | Medium | Medium | Medium | Low | Reload returns artifacts tab to files | `static/workspace.js:126-163` | Persist active tab |
| Expanded workspace dirs | `localStorage hermes-webui-expanded:<workspace>` | Presentation state | Low | Low | Low | Shared pref accepted | File tree collapses if key/restore fails | `static/workspace.js:108-123`; no focused test found | Add test backfill |
| Activity/tool disclosure state | `localStorage hermes-activity-disclosure:<session>:<activity>` plus live boolean | Presentation state; already persisted | Low | Low | Low | Shared pref accepted | Activity group reopens/closes unexpectedly if key unstable | `static/ui.js:5769-5830` | No immediate card; keep as reference pattern |
| Model selection | Session model, profile default, `localStorage hermes-webui-model-state`, pending per-session `sessionStorage` | Active session/profile default should win; stale browser prefs must not override active truth | Medium | Medium | Low | Medium: shared localStorage last-write-wins | Model chip/dropdown flips between tabs or after catalog refresh | `static/ui.js:960-1058`, `static/boot.js:1815-1846`, `tests/test_model_selection_refresh_persistence.py` | Add timestamp + storage listener |
| Profile/workspace settings | Backend settings/profile/workspace APIs plus browser mirrors | Backend/profile config is source; browser mirrors are presentation/default hints | Low | Medium | Low | Medium if one tab changes profile/settings while another is active | Topbar/workspace/model disagree between tabs | `static/boot.js`, `api/profiles.py`, `api/workspace.py`, `api/routes.py:6354-6402` | Cross-tab prefs notification after localStorage merge helpers |
| Approval / clarify cards | Runtime callbacks/adapter paths plus UI card state; clarify draft uses `sessionStorage` on expiry/terminal | Pending prompt should be runtime-owned and discoverable via adapter/journal over time | Medium | Medium | Medium/high on restart until adapter durability lands | Medium | Approval/clarify prompt disappears or draft only survives in same tab | `static/messages.js:3040-3180`, `docs/rfcs/hermes-run-adapter-contract.md` | RuntimeAdapter durability follow-up |
| Voice/mic recording | In-memory `_voiceModeActive`, `_micActive`, recorded blob to `S.pendingFiles` | Explicitly transient unless converted to pending file | High mid-record | High | High | Per-tab only | Recording lost on reload or navigation | `static/boot.js:438-706`, `static/boot.js:498-501` | Add UX warning/guard before unload while recording |
| Session events / sidebar invalidation | In-process version counter + maxsize=1 queue | Coarse invalidation; missed event should force refetch, not carry detailed truth | Low for sidebar | Low | Medium on WebUI restart | Medium for prefs not covered | Sidebar updates lag or other tabs miss settings changes | `api/session_events.py:1-45` | Extend only after deciding event types |

## Loss taxonomy

1. Volatile presentation loss. DOM or `S` state has no persistence tier. Examples: message scroll, workspace active tab, voice recording state.
2. Runtime truth loss. Runtime ownership lives in process-local structures and must be recovered from journal/session state after restart. Examples: `STREAMS`, `CANCEL_FLAGS`, `AGENT_INSTANCES`.
3. Canonical-session drift. URL, query, `localStorage`, sidebar row, and lineage helpers resolve different “current” sessions. Existing contracts define the cure: one requested-ID resolver.
4. Split-brain tabs. Each tab has its own URL/session owner, which is good, but shared `localStorage` keys can still clobber shared preferences or read-state maps.
5. Stale writeback. A canceled/rotated stream writes a terminal result/error into a session it no longer owns. Existing `test_stale_stream_writeback.py` coverage is important and should remain in any runtime refactor.
6. Replay duplication. Live SSE and replay paths append the same assistant/thinking/tool/progress state twice, or replay uses a flatter/different renderer. The run-state contract calls this out explicitly.
7. Lifetime mismatch. `sessionStorage` is used for data users may consider “draft intent” or “queued intent”; it survives reload but not tab close. This is valid only if the UI labels it honestly.
8. Backend/browser mismatch. Server settings/profile/workspace are durable, browser mirrors are stale hints. Boot logic must choose the server/session source unless a browser preference is explicitly intended to win.
9. Coverage gap. Many fixes are protected by static string tests; missing flows need focused regression tests before further stabilization.

## Phased stabilization plan

### Phase 0 — Freeze contracts and add probes

Goal: make current behavior measurable before implementation.

Tasks:

- Add a state-surface smoke checklist to `TESTING.md` or this audit’s follow-up cards: reload, session switch, bfcache restore, WebUI restart, reconnect, two tabs.
- Add test fixtures that can simulate browser storage maps without requiring a live browser for every case.
- Keep no-runtime-change rule for this phase.

Acceptance:

- Tests or checklists cover each row in the matrix at least once.
- No production state ownership changes.

### Phase 1 — Low-risk presentation persistence

Goal: stop the most visible “forgot where I was” losses without touching runtime truth.

Tasks:

- Persist message scroll per tab/session in `sessionStorage`; save on scroll/pagehide/visibilitychange, restore after `loadSession()` render, clamp using the existing `_restoreMessageScrollSnapshot()` path.
- Persist `_workspacePanelActiveTab` per browser/workspace, matching existing `localStorage` patterns for panel mode and expanded dirs.
- Add focused tests for scroll restore, workspace active tab restore, and expanded dir restore.

Acceptance:

- Reload and bfcache restore keep reading position for the active session.
- Session switch and back switch restore the per-tab scroll point.
- No change to transcript, run journal, or runtime control paths.

### Phase 2 — Finish composer attachment draft restore

Goal: make the existing draft API’s `files` field useful.

Tasks:

- Rehydrate `S.pendingFiles` in `_restoreComposerDraft()` for safe file references or explicitly model them as reattachable metadata, not raw browser `File` objects.
- Save attachment metadata on draft updates, using the existing `_saveComposerDraft(sid, text, files)` call sites.
- If full file rehydration is not technically possible for local `File` blobs, display a durable warning: text is restored, files must be reattached.

Acceptance:

- Text and file draft behavior is explicit and tested.
- Reload before send no longer silently drops attachments without user-facing notice.

### Phase 3 — Multi-tab merge and preference safety

Goal: reduce last-write-wins clobber from shared browser state.

Tasks:

- Add helper utilities for versioned `localStorage` JSON maps: read latest before write, merge by key, preserve newer `updated_at` fields.
- Apply helpers to viewed/unread maps and inflight snapshots.
- Add timestamped model preference state and a `storage` event handler that adopts a newer cross-tab model only when safe: no active stream, no unsent composer text, and no active session model override.

Acceptance:

- Two tabs updating different sessions do not lose unread/viewed map entries.
- Model selection does not unexpectedly override an active session’s model.
- Tests cover a simulated two-tab interleaving.

### Phase 4 — Canonical session resolver hardening

Goal: ensure all entry points resolve the same logical conversation.

Tasks:

- Centralize requested-ID resolution for route, query, `localStorage`, sidebar, direct open, and boot restore.
- Keep archived `pre_compression_snapshot` browsing explicit and separate from normal chat navigation.
- Add fixture sessions with parent snapshot, continuation tip, direct non-snapshot session, missing session, and archive-inspection mode.

Acceptance:

- URL `/session/<snapshot-parent>` opens the visible continuation during normal navigation when a continuation exists.
- Explicit archive inspection can still open the snapshot record.
- Query aliases and localStorage produce the same target as the route path.

### Phase 5 — RuntimeAdapter / journal durability for controls

Goal: reduce WebUI process-local runtime truth over time.

Tasks:

- Keep `STREAMS`/`CANCEL_FLAGS` as compatibility plumbing only; expose active run status through adapter/journal-backed observation.
- Make approval/clarify pending prompts discoverable after reconnect/restart where feasible.
- Keep `/queue` browser-side unless a maintainer explicitly chooses a durable server queue; the current RuntimeAdapter RFC treats `queue_message(...)` as staged, not a required server scheduler.

Acceptance:

- Reopen session can discover active/completed/interrupted run state without trusting process-local `STREAMS` as authority.
- Cancel/approval/clarify/goal control paths have adapter-shaped tests before moving ownership.
- WebUI restart during active run shows either replayed journal state or an explicit interrupted/stale diagnostic, never fake continuity.

### Phase 6 — Cross-tab server/prefs notification

Goal: tabs converge without reload for non-transcript settings.

Tasks:

- Either extend `/api/session-events` with typed reasons beyond sidebar invalidation or add a narrow `BroadcastChannel('hermes-webui-prefs')` for browser-local prefs.
- Keep session navigation per-tab by URL; do not reintroduce `localStorage` as a global active-session bus.
- Notify tabs of profile/settings/model catalog changes, with safe adoption rules.

Acceptance:

- Changing profile/settings in one tab updates passive topbar/chrome in another without forcing chat navigation.
- Active stream/composer state is not interrupted by cross-tab preference events.

## Recommended Kanban card dependency graph

These are proposed follow-up cards; IDs should be assigned by the board when created.

1. `webui-state-tests-baseline`
   - Scope: add/collect regression coverage for scroll restore, active workspace tab restore, expanded dirs, two-tab read-state merge, active reload/reconnect, and restart stale diagnostics.
   - Depends on: this audit.
   - Unblocks: all implementation cards.

2. `webui-scroll-and-panel-state-persistence`
   - Scope: per-tab message scroll persistence plus workspace active-tab persistence.
   - Depends on: `webui-state-tests-baseline` scroll/panel cases.
   - Unblocks: broader presentation-state cleanup.

3. `webui-composer-attachment-draft-restore`
   - Scope: finish `composer_draft.files` UX: restore safe metadata or warn clearly when files must be reattached.
   - Depends on: `webui-state-tests-baseline` draft/attachment cases.
   - Unblocks: send/queue lifetime documentation.

4. `webui-localstorage-merge-helpers`
   - Scope: versioned/merge-safe localStorage helpers for viewed/unread maps, inflight snapshots, and model prefs.
   - Depends on: `webui-state-tests-baseline` two-tab cases.
   - Unblocks: cross-tab prefs notification.

5. `webui-canonical-session-resolver-hardening`
   - Scope: make route/query/localStorage/sidebar/direct-open boot paths share one requested-ID resolver with explicit archive mode.
   - Depends on: existing canonical-session RFC; baseline lineage fixtures.
   - Unblocks: further session-routing or compression-lineage UX work.

6. `webui-runtime-control-durability-gate`
   - Scope: adapter/journal-backed status for cancel/approval/clarify/goal controls; no broad runner move until tests prove response-shape parity.
   - Depends on: RuntimeAdapter RFC gates and run journal tests.
   - Unblocks: process-local runtime ownership migration.

7. `webui-cross-tab-prefs-notification`
   - Scope: typed server/session events or BroadcastChannel for passive profile/settings/model changes.
   - Depends on: `webui-localstorage-merge-helpers`.
   - Unblocks: lower-noise multi-tab UX.

Suggested order:

```text
this audit
  -> webui-state-tests-baseline
       -> webui-scroll-and-panel-state-persistence
       -> webui-composer-attachment-draft-restore
       -> webui-localstorage-merge-helpers
            -> webui-cross-tab-prefs-notification
       -> webui-canonical-session-resolver-hardening
       -> webui-runtime-control-durability-gate
```

## Non-goals

- No runtime code changed by this audit.
- No new server-side queue is recommended merely for adapter symmetry.
- No archived compression snapshot deletion or session-file rewrite.
- No Open WebUI assumptions; this audit is for the Hermes WebUI repository.
- No credential, token, or `.env` state is preserved or discussed.

## Verification notes

Evidence inspected:

- Source files: `static/ui.js`, `static/sessions.js`, `static/messages.js`, `static/boot.js`, `static/workspace.js`, `api/routes.py`, `api/config.py`, `api/run_journal.py`, `api/session_events.py`, and searched `api/streaming.py`.
- Contract files: `docs/rfcs/webui-run-state-consistency-contract.md`, `docs/rfcs/canonical-session-resolution.md`, `docs/rfcs/hermes-run-adapter-contract.md`.
- Test families: `tests/test_session_cross_tab_sync.py`, `tests/test_session_stream_state_alignment.py`, `tests/test_stale_stream_cleanup.py`, `tests/test_stale_stream_pending_recovery.py`, `tests/test_stale_stream_writeback.py`, `tests/test_inflight_stream_reuse.py`, `tests/test_inflight_purge_missing_sessions.py`, `tests/test_model_selection_refresh_persistence.py`, `tests/test_sprint37.py`, and related static tests.

Known gaps called out above are based on source search and existing test names; they should be converted into focused tests before implementation cards land.
