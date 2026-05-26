"""Regression tests for queued-message visibility across session switches.

Queued follow-up messages are session-scoped, but their chips render into one
shared DOM flyout. Switching to another chat can hide/clear that shared DOM;
switching back must rebuild it even when the queued data fingerprint did not
change.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def test_queue_render_cache_is_scoped_to_dom_owner_session():
    """Same queue data is not enough to skip render after another session hid the DOM."""
    assert "let _queueRenderedSid" in UI_JS, (
        "queue chip render cache must track which session currently owns the shared queue DOM"
    )
    render_idx = UI_JS.find("function _renderQueueChips(sid)")
    assert render_idx >= 0
    render_body = UI_JS[render_idx:UI_JS.find("function _updateQueuePill", render_idx)]
    assert "_queueRenderedSid===sid" in render_body, (
        "_renderQueueChips must only skip same-key renders when the shared DOM already belongs to this sid"
    )
    assert "_queueRenderedSid=sid" in render_body, (
        "successful queue chip rebuild must stamp the shared DOM owner sid"
    )


def test_switching_to_session_without_queue_invalidates_previous_queue_dom_owner():
    """Hiding the shared queue DOM for chat B must not leave chat A's cache fresh."""
    update_idx = UI_JS.find("function updateQueueBadge(sessionId)")
    assert update_idx >= 0
    update_body = UI_JS[update_idx:UI_JS.find("const TOAST_DEFAULT_MS", update_idx)]
    assert "delete _queueRenderKeys[_queueRenderedSid]" in update_body, (
        "when global queue DOM is hidden/cleared, previous owner cache must be invalidated"
    )
    assert "_queueRenderedSid=null" in update_body, (
        "when global queue DOM is hidden/cleared, DOM owner must be cleared"
    )


def test_loading_inflight_session_refreshes_queue_badge():
    """Switching back to an active chat with queued follow-ups must show its queue chips."""
    inflight_idx = SESSIONS_JS.find("if(INFLIGHT[sid]){")
    assert inflight_idx >= 0
    else_idx = SESSIONS_JS.find("}else{", inflight_idx)
    assert else_idx > inflight_idx
    inflight_body = SESSIONS_JS[inflight_idx:else_idx]
    assert "updateQueueBadge(sid);" in inflight_body, (
        "loadSession() INFLIGHT branch must refresh queued-message chips after rebuilding the active pane"
    )
