import threading

import api.config as config
import api.routes as routes


class _FakeSession:
    def __init__(self):
        self.session_id = "race-session"
        self.title = "Untitled"
        self.messages = []
        self.context_messages = []
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.workspace = "/tmp"
        self.model = "test-model"
        self.model_provider = None
        self.worktree_path = None
        self.saved_stream_ids = []

    def save(self, *args, **kwargs):
        self.saved_stream_ids.append(self.active_stream_id)


class _FakeThread:
    started = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def start(self):
        # Do not run agent workers in unit tests; record the worker request only.
        self.started.append((self.args, self.kwargs))


def test_chat_start_serializes_duplicate_start_before_stream_registration(monkeypatch):
    """Concurrent /api/chat/start calls must not create two active streams.

    Regression: the duplicate-stream guard used to run before the per-session
    lock. A second request could observe active_stream_id=None while the first
    request was blocked inside pending-state preparation, then start a second
    worker after the first released the lock. That produced duplicate live
    assistant turns and false "Response interrupted" recovery markers.
    """
    session = _FakeSession()
    results = {}
    first_prepare_entered = threading.Event()
    release_first_prepare = threading.Event()
    prepare_lock = threading.Lock()
    prepare_calls = {"count": 0}
    original_prepare = routes._prepare_chat_start_session_for_stream

    def delayed_prepare(*args, **kwargs):
        with prepare_lock:
            prepare_calls["count"] += 1
            call_number = prepare_calls["count"]
        if call_number == 1:
            first_prepare_entered.set()
            assert release_first_prepare.wait(2), "test did not release first prepare gate"
        return original_prepare(*args, **kwargs)

    real_thread = threading.Thread
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", delayed_prepare)
    monkeypatch.setattr(routes.threading, "Thread", _FakeThread)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **k: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **k: None)
    try:
        import api.turn_journal as turn_journal
        monkeypatch.setattr(turn_journal, "append_turn_journal_event", lambda *a, **k: {})
    except Exception:
        pass

    with config.STREAMS_LOCK:
        old_streams = dict(config.STREAMS)
        config.STREAMS.clear()
    old_session_locks = dict(config.SESSION_AGENT_LOCKS)
    config.SESSION_AGENT_LOCKS.clear()
    _FakeThread.started.clear()

    def start(label, message):
        results[label] = routes._start_chat_stream_for_session(
            session,
            msg=message,
            attachments=[],
            workspace="/tmp",
            model="test-model",
            model_provider=None,
            normalized_model=False,
        )

    try:
        first = real_thread(target=start, args=("first", "first prompt"))
        first.start()
        assert first_prepare_entered.wait(2), "first start did not reach prepare gate"

        second = real_thread(target=start, args=("second", "second prompt"))
        second.start()
        # Give the second request time to reach the session lock while the first
        # request still has not set active_stream_id.
        threading.Event().wait(0.05)
        release_first_prepare.set()
        first.join(2)
        second.join(2)

        assert not first.is_alive()
        assert not second.is_alive()

        statuses = sorted(int(result.get("_status", 200) or 200) for result in results.values())
        assert statuses == [200, 409]
        assert sum(1 for result in results.values() if result.get("stream_id")) == 1
        with config.STREAMS_LOCK:
            active_stream_ids = list(config.STREAMS)
        assert len(active_stream_ids) == 1
        assert session.active_stream_id == active_stream_ids[0]
        assert len(_FakeThread.started) == 1
    finally:
        release_first_prepare.set()
        with config.STREAMS_LOCK:
            config.STREAMS.clear()
            config.STREAMS.update(old_streams)
        config.SESSION_AGENT_LOCKS.clear()
        config.SESSION_AGENT_LOCKS.update(old_session_locks)
