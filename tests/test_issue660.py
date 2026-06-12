"""
Tests for #660: session queue persistence across page refresh.

The queue is stored to sessionStorage when entries are added/removed,
and restored from sessionStorage on session load when the agent is idle.
"""
import pathlib

UI_JS = pathlib.Path(__file__).parent.parent / 'static' / 'ui.js'
SESSIONS_JS = pathlib.Path(__file__).parent.parent / 'static' / 'sessions.js'

ui_src = UI_JS.read_text(encoding='utf-8')
sess_src = SESSIONS_JS.read_text(encoding='utf-8')


class TestQueuePersistence:
    """queueSessionMessage persists to sessionStorage."""

    def test_queue_writes_to_session_storage(self):
        """queueSessionMessage must write to sessionStorage after enqueueing."""
        assert "sessionStorage.setItem('hermes-queue-'+sid" in ui_src

    def test_queue_stamps_queued_at_timestamp(self):
        """Each queue entry must have a _queued_at timestamp for stale-entry detection."""
        assert '_queued_at' in ui_src

    def test_shift_removes_from_session_storage(self):
        """shiftQueuedSessionMessage must remove/update sessionStorage on dequeue."""
        assert "sessionStorage.removeItem('hermes-queue-'+sid)" in ui_src

    def test_shift_updates_session_storage_when_items_remain(self):
        """When queue still has items after shift, sessionStorage is updated (not removed)."""
        # After shift: if queue still has items, update storage with remaining
        assert "sessionStorage.setItem('hermes-queue-'+sid, JSON.stringify(q))" in ui_src
        # Counts: should appear in both add and update paths (2 occurrences minimum)
        count = ui_src.count("sessionStorage.setItem('hermes-queue-'+sid")
        assert count >= 2, f"Expected >=2 sessionStorage.setItem calls, found {count}"


class TestQueueRestore:
    """Reload recovery is owned by the backend-canonical reconcile, not the
    legacy sessionStorage restore-to-composer path (which duplicated the same
    entry into the composer AND the live queue)."""

    def test_legacy_restore_to_composer_removed(self):
        """loadSession must not copy a queued entry into the composer."""
        assert "_msg.value=_first.text" not in sess_src

    def test_legacy_restore_toast_removed(self):
        """The 'Queued message restored' toast belonged to the removed path."""
        assert 'restored' not in sess_src.lower() or 'review and send when ready' not in sess_src

    def test_loadsession_reconciles_backend_queue(self):
        """loadSession must reconcile the queue from the backend-canonical store."""
        assert 'reconcileSessionQueue(sid)' in sess_src

    def test_loadsession_does_not_read_legacy_storage_key(self):
        """sessions.js must not read the optimistic cache key directly; ui.js owns it."""
        assert "sessionStorage.getItem('hermes-queue-'" not in sess_src

