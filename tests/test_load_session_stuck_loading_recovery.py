"""Regression tests for stuck `Loading conversation...` session switches.

Bug shape: loadSession() writes a loading placeholder, sets `_loadingSessionId`,
then a later synchronous render/UI error can abort the async function before the
success-path cleanup at the bottom. The stale `_loadingSessionId` makes sidebar
click handlers ignore other sessions, so the UI appears stuck until page reload.
"""
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


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


def _compact(src: str) -> str:
    return re.sub(r"\s+", "", src)


def test_load_session_clears_loading_lock_in_finally_after_placeholder():
    """Unexpected errors after switching must not strand sidebar navigation."""
    body = _function_body(SESSIONS_JS, "async function loadSession")
    lock_idx = body.index("_loadingSessionId = sid;")
    stop_idx = body.index("stopApprovalPolling", lock_idx)
    tail = body[lock_idx:]
    compact_tail = _compact(tail)

    # The outer guard must start before the first post-lock side effect. Inner
    # try/catch blocks for individual fetches are too narrow: renderMessages(),
    # syncTopbar(), updateQueueBadge(), etc. can still throw later.
    assert re.search(r"_loadingSessionId\s*=\s*sid;\s*try\s*{", tail), (
        "loadSession must wrap all post-_loadingSessionId side effects in an outer try"
    )
    assert body.index("try", lock_idx) < stop_idx, (
        "outer try must begin before stopApprovalPolling()/placeholder side effects"
    )
    assert "}finally{if(_loadingSessionId===sid)_loadingSessionId=null;" in compact_tail, (
        "loadSession must clear _loadingSessionId in an outer finally so a thrown "
        "render/UI error cannot block later sidebar clicks"
    )


def test_load_session_unexpected_error_replaces_loading_placeholder_before_rethrow():
    """A bad message/render error should show recoverable failure, not permanent loading."""
    body = _function_body(SESSIONS_JS, "async function loadSession")
    lock_idx = body.index("_loadingSessionId = sid;")
    compact_tail = _compact(body[lock_idx:])

    assert "catch(e){" in compact_tail, "outer loadSession guard must catch unexpected post-lock errors"
    assert "Loadingconversation" in compact_tail, (
        "unexpected-error handler should only overwrite the transient Loading conversation placeholder"
    )
    assert "Failedtoloadconversation" in compact_tail, (
        "unexpected-error handler must replace the loading placeholder with a recoverable failure message"
    )
    assert re.search(r"catch\(e\).*throwe", compact_tail), (
        "unexpected errors should still be rethrown after UI/lock recovery for console diagnostics"
    )
