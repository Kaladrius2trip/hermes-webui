"""
Tests for session queue persistence across page refresh and tab restore.

#660 introduced sessionStorage persistence. #3108 hardens it by mirroring queue
state to localStorage and restoring from the durable copy when sessionStorage is
missing after browser tab/process restore.
"""
import pathlib

UI_JS = pathlib.Path(__file__).parent.parent / 'static' / 'ui.js'
SESSIONS_JS = pathlib.Path(__file__).parent.parent / 'static' / 'sessions.js'

ui_src = UI_JS.read_text(encoding='utf-8')
sess_src = SESSIONS_JS.read_text(encoding='utf-8')


class TestQueuePersistence:
    """The fork's queue is backend-canonical: /api/session/queue is the
    durable store; sessionStorage is only an optimistic per-tab cache.
    (Upstream's dual session/localStorage helper architecture is superseded.)"""

    def test_queue_appends_to_backend(self):
        """queueSessionMessage must POST the entry to the backend store."""
        start = ui_src.find("function queueSessionMessage(sid, payload)")
        assert start != -1
        block = ui_src[start:start + 3000]
        assert "api('/api/session/queue'" in block

    def test_queue_keeps_optimistic_session_storage_cache(self):
        assert "sessionStorage.setItem('hermes-queue-'+sid" in ui_src

    def test_queue_stamps_queued_at_timestamp(self):
        """Each queue entry must have a _queued_at timestamp for stale-entry detection."""
        assert '_queued_at' in ui_src

    def test_ack_shifts_backend_store(self):
        """Consuming a queued item must ack the backend (tombstoned shift)."""
        assert "api('/api/session/queue/shift'" in ui_src

    def test_edit_paths_persist_through_backend_replace(self):
        """Queue edit/combine/delete paths must replace the backend store."""
        assert "_saveAndRefresh()" in ui_src
        assert "api('/api/session/queue/replace'" in ui_src


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

    def test_delete_session_clears_local_queue_copy(self):
        """Deleting a session must clear the local optimistic queue copy
        (fork equivalent of upstream's _clearPersistedSessionQueue)."""
        start = sess_src.find("async function deleteSession(sid, beforeDelete=null)")
        assert start != -1, "deleteSession block not found"
        block = sess_src[start:start + 6000]
        assert "_setSessionQueue(sid,[])" in block

