"""Regression coverage for the first state-loss reconciliation slice (#2361).

This slice promotes two pieces of presentation state from in-memory-only to
per-tab / per-browser persisted storage, without touching any runtime truth:

- Message scroll position: per-tab presentation state in ``sessionStorage``,
  keyed by session id. Saved on scroll / pagehide / visibilitychange, restored
  after the loadSession render for the same session, with no bottom snap.
- Workspace panel active tab: per-browser workspace preference in
  ``localStorage``, keyed by workspace (mirrors expanded-dir persistence).

These tests assert source structure, matching the existing scroll/viewport
regression suites in this repository.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    start = src.index(signature)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function body not found: {signature}")


def _scroll_listener_block() -> str:
    start = UI_JS.index("el.addEventListener('scroll'")
    return UI_JS[start : UI_JS.index("})();", start)]


# ── Message scroll: per-tab sessionStorage presentation state ──


def test_message_scroll_state_key_is_session_scoped_and_uses_session_storage():
    key = _function_body(UI_JS, "function _messageScrollStateKey")
    save = _function_body(UI_JS, "function _saveMessageScrollState")
    load = _function_body(UI_JS, "function _loadMessageScrollState")

    # Key is derived from the current (or explicitly passed) session id so each
    # session keeps an independent scroll offset.
    assert "S.session&&S.session.session_id" in key
    assert "'hermes-webui-msg-scroll:'" in key

    # Owner storage is sessionStorage (per tab), not localStorage or memory only.
    assert "sessionStorage.setItem" in save
    assert "_messageScrollStateKey()" in save
    assert "el.scrollTop" in save
    assert "sessionStorage.getItem" in load


def test_scroll_listener_persists_current_session_scroll_state():
    listener_block = _scroll_listener_block()
    assert "_saveMessageScrollState();" in listener_block, (
        "the #messages scroll listener must persist the current session's scroll "
        "offset so a refresh or tab restore returns the reader to position"
    )


def test_pagehide_and_visibilitychange_save_scroll_state():
    assert "addEventListener('pagehide',_saveMessageScrollState)" in UI_JS, (
        "scroll offset must be flushed on pagehide so a full unload still persists it"
    )
    # A hidden tab may be discarded before pagehide on mobile; flush on hide too.
    vis_idx = UI_JS.index("document.addEventListener('visibilitychange',()=>{ if(document.visibilityState==='hidden') _saveMessageScrollState(); })")
    assert vis_idx > 0


def test_loadsession_restores_persisted_scroll_after_render_same_session_no_snap():
    load_session = _function_body(SESSIONS_JS, "async function loadSession")
    restore = _function_body(UI_JS, "function _restoreMessageScrollStateForSession")

    # The idle render path renders, then restores the persisted offset. Upstream
    # may pass preserveScroll on same-session force reloads; the regression only
    # requires that restore happens after the render call.
    render_idx = load_session.rindex("syncTopbar();renderMessages(")
    restore_idx = load_session.index("_restoreMessageScrollStateForSession(sid)")
    assert render_idx < restore_idx, (
        "persisted scroll must be restored after messages render, not before"
    )

    # Restore is gated to the still-active session and must NOT snap to bottom.
    assert "const activeSid=S.session&&S.session.session_id" in restore
    assert "sid!==activeSid" in restore
    assert "_restoreMessageScrollSnapshot(snapshot)" in restore
    assert "scrollToBottom(" not in restore
    # Cancel any in-flight bottom settle so the restored offset wins.
    assert "_cancelBottomSettle" in restore


# ── Workspace active tab: per-workspace localStorage preference ──


def test_workspace_active_tab_key_is_workspace_scoped_localStorage():
    key = _function_body(WORKSPACE_JS, "function _wsActiveTabKey")
    save = _function_body(WORKSPACE_JS, "function _saveWorkspacePanelActiveTab")
    restore = _function_body(WORKSPACE_JS, "function _restoreWorkspacePanelActiveTab")

    # Scoped to the session's workspace, exactly like expanded-dir state.
    assert "S.session&&S.session.workspace" in key
    assert "'hermes-webui-wstab:'" in key

    assert "localStorage.setItem" in save
    assert "localStorage.getItem" in restore
    # Only the two known tab values are honored; anything else falls back to files.
    assert "'artifacts'" in restore
    assert "'files'" in restore


def test_switch_workspace_panel_tab_persists_choice():
    switch = _function_body(WORKSPACE_JS, "function switchWorkspacePanelTab")
    assert "_saveWorkspacePanelActiveTab();" in switch, (
        "switching the workspace panel tab must persist the choice per workspace"
    )


def test_load_dir_root_restores_active_tab_before_rendering_tabs():
    # loadDir contains template literals; use the next function as a stable slice
    # boundary instead of the generic brace counter.
    start = WORKSPACE_JS.index("async function loadDir")
    body = WORKSPACE_JS[start : WORKSPACE_JS.index("async function _refreshGitBadge", start)]

    # Restore happens in the root-load branch, alongside expanded-dir restore.
    expand_idx = body.index("_restoreExpandedDirs();")
    restore_idx = body.index("_restoreWorkspacePanelActiveTab();")
    apply_idx = body.index("switchWorkspacePanelTab(_workspacePanelActiveTab);")
    tree_idx = body.index("renderFileTree();")

    assert expand_idx < restore_idx < apply_idx < tree_idx, (
        "workspace active tab must be restored and applied to the DOM on root "
        "load before the file tree renders"
    )
