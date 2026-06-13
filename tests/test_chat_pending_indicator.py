"""The "waiting for the model" indicator fills the pre-token latency gap.

After send, the top run-activity card is gone (Phase C), so a reasoning model
on a large context showed a bare "running" with nothing for tens of seconds.
A pending indicator now shows during exactly the gap between stream-wire and
the first token/reasoning/tool event, and clears on output or any terminal
event.
"""

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_indicator_helpers_exist():
    assert "function showChatPendingIndicator()" in UI_JS
    assert "function hideChatPendingIndicator()" in UI_JS
    assert "id='chatPendingIndicator'" in UI_JS or 'id="chatPendingIndicator"' in UI_JS
    assert ".chat-pending{" in STYLE_CSS


def test_shown_when_stream_wired_with_no_output_yet():
    wire = MESSAGES_JS[MESSAGES_JS.index("function _wireSSE(source)"):]
    wire = wire[: wire.index("source.addEventListener('token'")]
    assert "showChatPendingIndicator()" in wire
    assert "!assistantText" in wire  # only when nothing has streamed yet


def test_hidden_on_first_output_events():
    for ev in ("token", "reasoning", "tool", "interim_assistant"):
        anchor = f"source.addEventListener('{ev}',"
        i = MESSAGES_JS.index(anchor)
        block = MESSAGES_JS[i:i + 400]
        assert "hideChatPendingIndicator()" in block, f"{ev} handler must clear the pending indicator"


def test_hidden_on_terminal_events_and_setbusy_false():
    for ev in ("done", "error", "cancel"):
        anchor = f"source.addEventListener('{ev}',"
        i = MESSAGES_JS.index(anchor)
        block = MESSAGES_JS[i:i + 400]
        assert "hideChatPendingIndicator()" in block, f"{ev} handler must clear the pending indicator"
    busy = UI_JS[UI_JS.index("function setBusy(v)"):]
    busy = busy[: busy.index("setComposerStatus('');")]
    assert "hideChatPendingIndicator()" in busy
