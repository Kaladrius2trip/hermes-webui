"""Perf regression: the post-turn display/context merge must stay near-linear.

A long compression-stitched session (thousands of messages) re-runs
_merge_display_messages_after_agent_result at the end of every turn. The
context-backfill loop used to recompute _message_identity (json.dumps + regex
per message) O(n^2) times and scan context slices, pinning a CPU core for
minutes — surfacing as a WebUI session stuck on "running" doing nothing.
"""

import time

from api.streaming import _merge_display_messages_after_agent_result


def _msgs(n, role_cycle=("user", "assistant")):
    out = []
    for i in range(n):
        role = role_cycle[i % len(role_cycle)]
        out.append({"role": role, "content": f"message number {i} body text", "timestamp": float(i)})
    return out


def test_large_session_merge_is_fast():
    n = 4000
    display = _msgs(n)
    # context-only divergence so the backfill branch (the former O(n^2) hot
    # loop) actually runs: prepend a context-only turn missing from display.
    context = [{"role": "user", "content": "context-only header", "timestamp": -1.0}] + _msgs(n)
    result = list(context) + [{"role": "user", "content": "newest turn", "timestamp": float(n)}]

    start = time.monotonic()
    merged = _merge_display_messages_after_agent_result(display, context, result, "newest turn")
    elapsed = time.monotonic() - start

    assert elapsed < 3.0, f"merge took {elapsed:.1f}s for {n} messages — O(n^2) regression"
    assert merged  # produced a transcript
    assert any(m.get("content") == "newest turn" for m in merged)


def test_context_only_turn_still_backfilled():
    # Behavioural guard: the optimization must preserve the backfill of a
    # context-only turn that is missing from the visible display.
    display = [
        {"role": "user", "content": "hello", "timestamp": 1.0},
        {"role": "assistant", "content": "hi there", "timestamp": 2.0},
    ]
    context = [
        {"role": "user", "content": "hello", "timestamp": 1.0},
        {"role": "user", "content": "hidden behind compaction", "timestamp": 1.5},
        {"role": "assistant", "content": "hi there", "timestamp": 2.0},
    ]
    result = list(context) + [{"role": "assistant", "content": "follow up", "timestamp": 3.0}]

    merged = _merge_display_messages_after_agent_result(display, context, result, "")
    contents = [m.get("content") for m in merged]
    assert "hidden behind compaction" in contents
    assert "hello" in contents and "hi there" in contents
