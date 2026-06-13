"""Regression tests for moving a queued message into /steer from the queue UI."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    start_idx = src.find(start)
    assert start_idx >= 0, f"{start!r} not found"
    end_idx = src.find(end, start_idx + len(start))
    assert end_idx >= 0, f"{end!r} not found after {start!r}"
    return src[start_idx:end_idx]


def test_queue_rows_render_a_per_item_steer_button():
    queue_body = _between(UI_JS, "function _renderQueueChips(sid)", "function _updateQueuePill")

    assert "queue-card-steer-btn" in queue_body, (
        "each queued row must render a visible per-item steer action"
    )
    assert "Send queued message as steer" in queue_body
    assert "_steerQueuedSessionMessage(sid,_entryTs,i)" in queue_body, (
        "steer button must target the clicked queued item, not the whole queue"
    )


def test_queue_item_steer_removes_item_and_refreshes_before_calling_steer():
    helper = _between(UI_JS, "async function _steerQueuedSessionMessage", "function _renderQueueChips")

    # Removal is now authoritative (tombstone + seq bump + persist + badge)
    # via the shared _removeQueuedSessionMessage helper, called before steering.
    remove_idx = helper.find("_removeQueuedSessionMessage(sid,entry)")
    steer_idx = helper.find("_trySteer(entryText,true,fallbackPayload)")
    send_idx = helper.find("await send()")

    assert remove_idx >= 0, "helper must remove the clicked queue item via the shared helper"
    assert "sessionStorage.setItem('hermes-queue'" not in helper
    assert "sessionStorage.removeItem('hermes-queue'" not in helper
    assert steer_idx > remove_idx, "active-stream steer must run after the queue item is removed"
    assert send_idx > remove_idx, "idle /steer send fallback must also run after queue removal"

    # The shared remover must tombstone + bump seq so a racing reconcile cannot
    # resurrect the steered/deleted item.
    remover = _between(UI_JS, "function _removeQueuedSessionMessage(sid, entry)", "async function _steerQueuedSessionMessage")
    assert "_queueNextSeq(sid)" in remover
    assert "_queueMarkConsumed(sid,id)" in remover
    assert "_persistSessionQueue(sid,liveQ)" in remover
    assert "updateQueueBadge(sid)" in remover


def test_queue_item_steer_blocks_file_bearing_items_without_removing_them():
    helper = _between(UI_JS, "async function _steerQueuedSessionMessage", "function _renderQueueChips")

    files_idx = helper.find("Array.isArray(entry.files)")
    guard_idx = helper.find("Queued items with attachments cannot be steered")
    remove_idx = helper.find("_removeQueuedSessionMessage(sid,entry)")

    assert files_idx >= 0, "helper must inspect queued attachments"
    assert guard_idx > files_idx, "file-bearing queued items need explicit blocked UX"
    assert remove_idx > guard_idx, "file-bearing items must return before queue removal"


def test_try_steer_accepts_original_queue_payload_for_fallback():
    helper = _between(UI_JS, "async function _steerQueuedSessionMessage", "function _renderQueueChips")
    try_steer = _between(COMMANDS_JS, "async function _trySteer", "async function cmdTitle")

    assert "fallbackPayload" in try_steer, (
        "queued-item steer needs _trySteer to requeue the original item on endpoint rejection"
    )
    assert "const fallbackFromComposer=!fallbackPayload" in try_steer, (
        "queued-item fallback must not clear unrelated composer pending files"
    )
    assert "queueSessionMessage(S.session.session_id,fallbackPayload||" in try_steer, (
        "fallback must preserve the queued item's model/model_provider/profile payload"
    )
    assert "model_provider:entry.model_provider||null" in helper
    assert "profile:entry.profile||S.activeProfile||'default'" in helper
