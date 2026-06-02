from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def _ui_js() -> str:
    return (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_queue_render_focus_guard_does_not_suppress_header_actions():
    """Header buttons live inside #queueChips, so the focus guard must not hide their updates."""
    ui = _ui_js()

    assert "function _queuePanelHasEditingFocus(inner){" in ui
    assert "if(_queuePanelHasEditingFocus(inner)) return;" in ui
    assert "inner.contains(document.activeElement)&&document.activeElement!==inner" not in ui

    helper = re.search(
        r"function _queuePanelHasEditingFocus\(inner\)\{(?P<body>.*?)\n\}", ui, re.S
    )
    assert helper, "expected a named helper for the queue-panel focus guard"
    body = helper.group("body")

    assert "active.isContentEditable" in body
    assert "TEXTAREA" in body
    assert "INPUT" in body
    assert "BUTTON" not in body
