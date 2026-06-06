"""Regression tests for auto title refresh status restoration."""

import json
import pathlib
import re
import shutil
import subprocess

import pytest


REPO_ROOT = pathlib.Path(__file__).parent.parent
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_function(src: str, name: str) -> str:
    marker = re.search(rf"function {re.escape(name)}\s*\(", src)
    assert marker is not None, f"missing JS function {name}"
    start = marker.start()
    brace_pos = src.find("{", marker.end())
    assert brace_pos >= 0, f"missing opening brace for {name}"
    depth = 1
    pos = brace_pos + 1
    while pos < len(src) and depth > 0:
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        pos += 1
    assert depth == 0, f"unterminated JS function {name}"
    return src[start:pos]


def _extract_sse_handler(src: str, event_name: str) -> str:
    marker = f"source.addEventListener('{event_name}'"
    start = src.find(marker)
    assert start >= 0, f"missing {event_name} SSE handler"
    brace_pos = src.find("{", start)
    assert brace_pos >= 0, f"missing opening brace for {event_name} handler"
    depth = 1
    pos = brace_pos + 1
    while pos < len(src) and depth > 0:
        ch = src[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        pos += 1
    assert depth == 0, f"unterminated {event_name} SSE handler"
    return src[start:pos]


def _run_node(script: str) -> str:
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "-e", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout


def test_title_refresh_status_event_restores_sidebar_status_on_success_and_error():
    """Terminal title_status events must clear the sidebar 'generating' status."""
    set_fn = _extract_function(SESSIONS_JS, "_setSessionTitleRefreshInFlight")
    apply_fn = _extract_function(SESSIONS_JS, "_applySessionTitleRefreshStatusEvent")
    terminal_statuses = [
        "refreshed",
        "refresh_skipped",
        "skipped",
        "fallback",
        "llm_ok",
        "llm_ok_aux",
        "llm_error",
        "llm_error_aux",
        "llm_invalid",
        "llm_invalid_aux",
    ]
    active_statuses = ["generating", "refreshing", "refresh_started"]
    script = f"""
const _titleRefreshInFlightSids = new Set();
let _allSessions = [{{session_id:'sid', title_refresh_inflight:false}}];
const S = {{session:{{session_id:'sid', title_refresh_inflight:false}}}};
let renders = 0;
function renderSessionListFromCache(){{ renders += 1; }}
{set_fn}
{apply_fn}
const terminalStatuses = {json.dumps(terminal_statuses)};
const activeStatuses = {json.dumps(active_statuses)};
for (const status of activeStatuses) {{
  _setSessionTitleRefreshInFlight('sid', false);
  _applySessionTitleRefreshStatusEvent({{session_id:'sid', status}}, 'fallback');
  if (!_titleRefreshInFlightSids.has('sid')) throw new Error(status + ' did not mark in-flight');
  if (!_allSessions[0].title_refresh_inflight) throw new Error(status + ' did not update cached row');
  if (!S.session.title_refresh_inflight) throw new Error(status + ' did not update active session');
}}
for (const status of terminalStatuses) {{
  _setSessionTitleRefreshInFlight('sid', true);
  _applySessionTitleRefreshStatusEvent({{session_id:'sid', status}}, 'fallback');
  if (_titleRefreshInFlightSids.has('sid')) throw new Error(status + ' left in-flight set stuck');
  if (_allSessions[0].title_refresh_inflight) throw new Error(status + ' left cached row stuck');
  if (S.session.title_refresh_inflight) throw new Error(status + ' left active session stuck');
}}
_setSessionTitleRefreshInFlight('fallback', true);
_applySessionTitleRefreshStatusEvent({{status:'refreshed'}}, 'fallback');
if (_titleRefreshInFlightSids.has('fallback')) throw new Error('fallback sid was not cleared');
if (renders <= 0) throw new Error('status changes did not re-render session list');
"""
    _run_node(script)


def test_title_status_sse_handler_applies_sidebar_restore_helper():
    """Success/error title_status SSE must update the same in-flight flag as the sidebar."""
    handler = _extract_sse_handler(MESSAGES_JS, "title_status")
    compact = re.sub(r"\s+", "", handler)
    assert "_applySessionTitleRefreshStatusEvent(d,activeSid)" in compact, (
        "title_status SSE handler must clear title-refresh sidebar state on terminal success/error events"
    )


def test_title_success_sse_event_clears_inflight_status_even_without_status_event():
    """The title event is the success fallback for older streams without title_status."""
    handler = _extract_sse_handler(MESSAGES_JS, "title")
    compact = re.sub(r"\s+", "", handler)
    assert "_setSessionTitleRefreshInFlight(activeSid,false)" in compact, (
        "title SSE success handler must clear title-refresh in-flight UI status"
    )
