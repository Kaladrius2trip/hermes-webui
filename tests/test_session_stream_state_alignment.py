"""Regression coverage for active stream state staying aligned across panes.

Bug shape: after switching away from a streaming chat and back, the chat pane
could lose transient state (thinking/status/tool activity), while the sidebar
could render the row as inactive even though the server still had runtime
stream fields. The root cause is split-brain liveness checks: some paths only
trusted ``is_streaming`` while the pane restore path trusted ``active_stream_id``
/ pending runtime fields.
"""
from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 1
    i = brace + 1
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[brace + 1 : i - 1]


def _compact(src: str) -> str:
    return re.sub(r"\s+", "", src)


def test_effective_streaming_uses_all_runtime_liveness_signals():
    """Sidebar streaming state must match pane restore liveness signals."""
    body = _function_body(SESSIONS_JS, "_isSessionEffectivelyStreaming")

    assert "s.is_streaming" in body
    assert "s.active_stream_id" in body, "active_stream_id must mark a row as streaming"
    assert "s.pending_user_message" in body, "pending user turn must mark a row as streaming"
    assert "s.pending_started_at" in body, "pending start timestamp must mark a row as streaming"
    assert "_isSessionLocallyStreaming(s)" in body


def test_stale_inflight_purge_keeps_rows_with_runtime_liveness():
    """Do not delete INFLIGHT just because ``is_streaming`` is temporarily false."""
    body = _function_body(SESSIONS_JS, "_purgeStaleInflightEntries")
    compact = _compact(body)

    assert "_isSessionEffectivelyStreaming(s)" in body, (
        "INFLIGHT cleanup should use the same effective liveness as the sidebar row"
    )
    assert "if(!_isSessionEffectivelyStreaming(s)){" in compact, (
        "Only sessions with no effective runtime liveness should be purged"
    )
    assert "if(!s.is_streaming){" not in compact, (
        "Raw is_streaming alone is too narrow and drops active_stream_id-only sessions"
    )


def test_streaming_poll_lifetime_uses_effective_liveness():
    """Polling must continue while any row has stream/runtime fields."""
    body = _function_body(SESSIONS_JS, "_applySessionListPayload")
    compact = _compact(body)

    assert "constisStreaming=_allSessions.some(s=>_isSessionEffectivelyStreaming(s));" in compact, (
        "streaming poll lifetime must include active_stream_id / pending runtime rows"
    )


def test_optimistic_merge_preserves_fetched_runtime_fields():
    """Fetched active rows must not be nulled when local optimistic state expires."""
    body = _function_body(SESSIONS_JS, "_mergeOptimisticFirstTurnSessions")
    compact = _compact(body)

    assert (
        "active_stream_id:fetchedIsServerIdle?null:(fetched.active_stream_id||(keepLocalOptimistic?local.active_stream_id:null))"
        in compact
    ), "server-fetched active_stream_id must survive even when local optimistic state is dropped"
    assert (
        "pending_user_message:fetchedIsServerIdle?null:(fetched.pending_user_message||(keepLocalOptimistic?local.pending_user_message:null))"
        in compact
    ), "server-fetched pending_user_message must survive even when local optimistic state is dropped"
    assert (
        "pending_started_at:fetchedIsServerIdle?null:(fetched.pending_started_at||(keepLocalOptimistic?local.pending_started_at:null))"
        in compact
    ), "server-fetched pending_started_at must survive even when local optimistic state is dropped"
    assert (
        "is_streaming:fetchedIsServerIdle?false:Boolean(fetched.is_streaming||fetched.active_stream_id||fetched.pending_user_message||fetched.pending_started_at||(keepLocalOptimistic&&Boolean(local.is_streaming||_isSessionLocallyStreaming(local))))"
        in compact
    ), "merged is_streaming must derive from fetched runtime fields plus kept local optimistic state"
