"""Backend-first contract for WebUI queued input state.

The browser may render an optimistic queue, but queued user input is canonicalized
and persisted by WebUI backend APIs so refresh/session-switch/multi-tab paths can
reconcile from one source of truth.
"""

from types import SimpleNamespace
from urllib.parse import urlparse


class _Capture:
    payload = None
    status = None


def _capture_routes(monkeypatch):
    import api.routes as routes

    cap = _Capture()
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: setattr(cap, "payload", payload)
        or setattr(cap, "status", status)
        or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: setattr(cap, "payload", {"error": message})
        or setattr(cap, "status", status)
        or True,
    )
    return routes, cap


def test_session_queue_store_normalizes_and_persists(tmp_path, monkeypatch):
    from api import session_queue

    monkeypatch.setattr(session_queue, "QUEUE_DIR", tmp_path / "session_queues")

    appended = session_queue.append_queue_item(
        "sess_a",
        {
            "text": "  queued turn  ",
            "files": [{"name": "note.txt", "size": 12}],
            "model": "model-a",
            "model_provider": "provider-a",
            "profile": "work",
            "unknown_frontend_only": "discard-me",
        },
    )

    assert appended["count"] == 1
    item = appended["queue"][0]
    assert item["text"] == "queued turn"
    assert item["files"] == [{"name": "note.txt", "size": 12}]
    assert item["model"] == "model-a"
    assert item["model_provider"] == "provider-a"
    assert item["profile"] == "work"
    assert item["id"] and item["_queue_id"] == item["id"]
    assert "unknown_frontend_only" not in item

    # New read path proves persistence, not just in-memory mutation.
    assert session_queue.list_queue("sess_a") == [item]


def test_session_queue_replace_and_shift_are_canonical_mutations(tmp_path, monkeypatch):
    from api import session_queue

    monkeypatch.setattr(session_queue, "QUEUE_DIR", tmp_path / "session_queues")

    first = session_queue.append_queue_item("sess_a", {"text": "first"})["queue"][0]
    second = session_queue.append_queue_item("sess_a", {"text": "second"})["queue"][1]

    replaced = session_queue.replace_queue(
        "sess_a",
        [
            {**second, "text": "second edited"},
            {**first, "text": "first edited"},
        ],
    )
    assert [item["text"] for item in replaced["queue"]] == ["second edited", "first edited"]

    shifted = session_queue.shift_queue_item("sess_a", item_id=second["id"])
    assert shifted["item"]["text"] == "second edited"
    assert [item["text"] for item in shifted["queue"]] == ["first edited"]

    cleared = session_queue.replace_queue("sess_a", [])
    assert cleared == {"ok": True, "session_id": "sess_a", "queue": [], "count": 0}
    assert session_queue.list_queue("sess_a") == []


def test_session_queue_rejects_unsafe_session_ids(tmp_path, monkeypatch):
    from api import session_queue

    monkeypatch.setattr(session_queue, "QUEUE_DIR", tmp_path / "session_queues")

    for bad_sid in ("../etc/passwd", "bad/id", "", None):
        try:
            session_queue.list_queue(bad_sid)
        except ValueError as exc:
            assert "invalid session_id" in str(exc)
        else:
            raise AssertionError(f"accepted unsafe session_id: {bad_sid!r}")


def test_session_queue_routes_expose_backend_canonical_state(tmp_path, monkeypatch):
    from api import session_queue

    monkeypatch.setattr(session_queue, "QUEUE_DIR", tmp_path / "session_queues")
    routes, cap = _capture_routes(monkeypatch)
    import api.profiles as profiles
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: SimpleNamespace(session_id=sid, profile="default"))

    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {
            "session_id": "sess_a",
            "item": {"text": "route queued", "model": "m1"},
        },
    )
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/queue")) is True
    assert cap.status == 200
    item = cap.payload["queue"][0]
    assert cap.payload["count"] == 1
    assert item["text"] == "route queued"

    assert routes.handle_get(object(), urlparse("/api/session/queue?session_id=sess_a")) is True
    assert cap.status == 200
    assert cap.payload["queue"] == [item]

    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {
            "session_id": "sess_a",
            "queue": [{**item, "text": "route edited"}],
        },
    )
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/queue/replace")) is True
    assert cap.payload["queue"][0]["text"] == "route edited"

    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler: {"session_id": "sess_a", "item_id": item["id"]},
    )
    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/queue/shift")) is True
    assert cap.payload["item"]["text"] == "route edited"
    assert cap.payload["queue"] == []



def test_session_queue_shift_tombstones_unknown_optimistic_id(tmp_path, monkeypatch):
    from api import session_queue

    monkeypatch.setattr(session_queue, "QUEUE_DIR", tmp_path / "session_queues")
    shifted = session_queue.shift_queue_item("sess_race", item_id="optimistic-1")
    assert shifted["not_found"] is True
    assert shifted["queue"] == []

    late = session_queue.append_queue_item(
        "sess_race",
        {"id": "optimistic-1", "_queue_id": "optimistic-1", "text": "already sent"},
    )
    assert late["ignored"] is True
    assert late["queue"] == []
    assert session_queue.list_queue("sess_race") == []


def test_frontend_queue_reconcile_preserves_unconfirmed_local_items():
    src = open("static/ui.js", encoding="utf-8").read()
    assert "const _queuePendingIds" in src
    assert "function _mergeBackendQueueWithPendingLocal" in src
    assert "function _queueResyncMissingLocalItem" in src
    reconcile_fn = src.split("async function reconcileSessionQueue", 1)[1].split(
        "function _persistSessionQueue", 1
    )[0]
    assert "_mergeBackendQueueWithPendingLocal" in reconcile_fn
    merge_fn = src.split("function _mergeBackendQueueWithPendingLocal", 1)[1].split(
        "function _setSessionQueue", 1
    )[0]
    assert "if(id&&!seen[id])" in merge_fn
    assert "_queueResyncMissingLocalItem(sid,item)" in merge_fn
    resync_fn = src.split("function _queueResyncMissingLocalItem", 1)[1].split(
        "function _mergeBackendQueueWithPendingLocal", 1
    )[0]
    assert "api('/api/session/queue'" in resync_fn
    assert "r&&r.ignored&&r.consumed" in resync_fn
    shift_fn = src.split("function shiftQueuedSessionMessage", 1)[1].split(
        "function getQueuedSessionCount", 1
    )[0]
    assert "_queueClearPending(sid,next)" in shift_fn
    assert "r&&r.not_found" in shift_fn

def test_frontend_queue_helpers_call_backend_queue_api():
    src = open("static/ui.js", encoding="utf-8").read()
    sessions_src = open("static/sessions.js", encoding="utf-8").read()

    assert "async function reconcileSessionQueue" in src
    assert "/api/session/queue?session_id=" in src
    assert "api('/api/session/queue'" in src
    assert "api('/api/session/queue/replace'" in src
    assert "api('/api/session/queue/shift'" in src

    queue_fn = src.split("function queueSessionMessage", 1)[1].split(
        "function shiftQueuedSessionMessage", 1
    )[0]
    assert "api('/api/session/queue'" in queue_fn
    assert "sessionStorage.setItem('hermes-queue-'+sid" in queue_fn

    shift_fn = src.split("function shiftQueuedSessionMessage", 1)[1].split(
        "function getQueuedSessionCount", 1
    )[0]
    assert "api('/api/session/queue/shift'" in shift_fn
    assert "_setSessionQueue(sid,q)" in shift_fn

    assert "await reconcileSessionQueue(sid" in sessions_src


def test_session_queue_routes_reject_other_profile_sessions(tmp_path, monkeypatch):
    from api import session_queue
    import api.profiles as profiles

    monkeypatch.setattr(session_queue, "QUEUE_DIR", tmp_path / "session_queues")
    routes, cap = _capture_routes(monkeypatch)
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: SimpleNamespace(session_id=sid, profile="work"))
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": "sess_a", "item": {"text": "hidden"}})

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/queue")) is True
    assert cap.status == 404
    assert routes.handle_get(object(), urlparse("/api/session/queue?session_id=sess_a")) is True
    assert cap.status == 404
    assert session_queue.list_queue("sess_a") == []
