from pathlib import Path
from types import SimpleNamespace

import api.models as models
import api.routes as routes
import api.streaming as streaming
from api.models import SESSIONS, Session


REPO = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _capture_post(monkeypatch, body):
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "_forced_title_context_window", lambda: 1, raising=False)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, msg, status=400: captured.update(
            payload={"error": msg},
            status=status,
        )
        or True,
    )
    return captured


def _isolate_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    SESSIONS.clear()
    return session_dir


def _session(session_id="forcetitle1", *, title="Manual title", messages=None):
    messages = messages if messages is not None else [
        {"role": "user", "content": "Can we add a context menu title refresh?"},
        {"role": "assistant", "content": "Yes, add a forced title action and API endpoint."},
    ]
    s = Session(
        session_id=session_id,
        title=title,
        messages=messages,
        llm_title_generated=False,
    )
    SESSIONS[session_id] = s
    s.save()
    return s


def test_force_title_route_updates_default_title_and_marks_llm_generated(tmp_path, monkeypatch):
    """Explicit user action should generate a fresh title for default/generated titles now."""
    _isolate_session_store(tmp_path, monkeypatch)
    session = _session(title="Untitled")
    captured = _capture_post(monkeypatch, {"session_id": session.session_id})
    generated = []

    def fake_generate(session_obj, **kwargs):
        generated.append((session_obj.session_id, kwargs.get("recent_exchange_limit")))
        return "Forced Context Menu Titles", "llm_aux", ""

    monkeypatch.setattr(routes, "generate_session_title_for_session", fake_generate)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda reason, **kwargs: None)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/title/refresh")) is True

    assert generated == [(session.session_id, 1)]
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["status"] == "llm_aux"
    assert captured["payload"]["session"]["title"] == "Forced Context Menu Titles"
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.title == "Forced Context Menu Titles"
    assert saved.llm_title_generated is True


def test_force_title_route_uses_configured_context_window(tmp_path, monkeypatch):
    """Manual title refresh should pass the configured context window into canonical generation."""
    _isolate_session_store(tmp_path, monkeypatch)
    messages = [
        {"role": "user", "content": "Discuss lunch options"},
        {"role": "assistant", "content": "Pizza or salad would work."},
        {"role": "user", "content": "Plan WebUI forced title context"},
        {"role": "assistant", "content": "Use recent configured exchanges."},
        {"role": "user", "content": "Also prevent stale one-line titles"},
        {"role": "assistant", "content": "Collect enough recent topic evidence."},
    ]
    session = _session(title="Untitled", messages=messages)
    captured = _capture_post(monkeypatch, {"session_id": session.session_id})
    monkeypatch.setattr(routes, "_forced_title_context_window", lambda: 2)
    generated = []

    def fake_generate(session_obj, **kwargs):
        generated.append((session_obj.session_id, kwargs.get("recent_exchange_limit")))
        return "Recent Context Titles", "llm_aux", ""

    monkeypatch.setattr(routes, "generate_session_title_for_session", fake_generate)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda reason, **kwargs: None)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/title/refresh")) is True

    assert captured["status"] == 200
    assert generated == [(session.session_id, 2)]


def test_adaptive_title_refresh_uses_recent_configured_exchange_window(monkeypatch):
    """Automatic refresh should pass the same last-N exchange window it uses as cadence."""
    messages = [
        {"role": "user", "content": "Old topic one"},
        {"role": "assistant", "content": "Old answer one"},
        {"role": "user", "content": "Old topic two"},
        {"role": "assistant", "content": "Old answer two"},
        {"role": "user", "content": "Recent title context three"},
        {"role": "assistant", "content": "Recent answer three"},
        {"role": "user", "content": "Recent title context four"},
        {"role": "assistant", "content": "Recent answer four"},
    ]
    session = Session(
        session_id="autotitlewindow",
        title="Existing LLM Title",
        messages=messages,
        llm_title_generated=True,
    )
    monkeypatch.setattr(streaming, "_get_title_refresh_interval", lambda: 2)
    scheduled = {}

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            scheduled["target"] = target
            scheduled["args"] = args
            scheduled["daemon"] = daemon

        def start(self):
            scheduled["started"] = True

    monkeypatch.setattr(streaming.threading, "Thread", FakeThread)

    streaming._maybe_schedule_title_refresh(session, lambda *_args: None, agent=None)

    assert scheduled["started"] is True
    user_context = scheduled["args"][1]
    assistant_context = scheduled["args"][2]
    assert "Recent title context three" in user_context
    assert "Recent title context four" in user_context
    assert "Opening goal User: Old topic one" in user_context
    assert "Recent recurring topic User 1: Old topic two" in user_context
    assert "Latest exchange User: Recent title context four" in user_context
    assert "Recent answer three" in assistant_context
    assert "Recent answer four" in assistant_context
    assert "Opening goal Assistant: Old answer one" in assistant_context
    assert "Latest exchange Assistant: Recent answer four" in assistant_context


def test_force_title_route_titles_sessions_without_assistant_reply(tmp_path, monkeypatch):
    """Upstream contract (audit streaming-005): a session whose assistant turn
    errored, is tool-call-only, or hasn't answered yet must still be titleable
    from the user text alone instead of 422 empty_assistant_message."""
    _isolate_session_store(tmp_path, monkeypatch)
    session = _session(
        title="Untitled",
        messages=[{"role": "user", "content": "Refactor the websocket reconnect backoff logic"}],
    )
    captured = _capture_post(monkeypatch, {"session_id": session.session_id})

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/title/refresh")) is True

    assert captured["status"] == 200
    assert captured["payload"].get("title")
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.title == captured["payload"]["title"]


def test_force_title_route_rejects_duplicate_inflight_request(tmp_path, monkeypatch):
    """A second manual title request should not queue or race another active request."""
    _isolate_session_store(tmp_path, monkeypatch)
    session = _session()
    captured = _capture_post(monkeypatch, {"session_id": session.session_id})
    monkeypatch.setattr(routes, "_generate_forced_session_title", lambda *_args: ("Should Not Run", "llm_aux", ""))

    routes._TITLE_REFRESH_INFLIGHT.add(session.session_id)
    try:
        assert routes.handle_post(object(), SimpleNamespace(path="/api/session/title/refresh")) is True
    finally:
        routes._TITLE_REFRESH_INFLIGHT.discard(session.session_id)

    assert captured["status"] == 409
    assert captured["payload"]["status"] == "already_generating"
    assert "already generating" in captured["payload"]["error"].lower()
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.title == "Manual title"


def test_session_context_menu_exposes_force_title_action():
    assert "/api/session/title/refresh" in SESSIONS_JS
    assert "session_title_regenerate" in SESSIONS_JS
    assert "session_title_regenerate_desc" in SESSIONS_JS
    assert "session_title_regenerated" in SESSIONS_JS
    assert "session_generate_title" not in SESSIONS_JS
    assert "_forceGenerateSessionTitle(session)" in SESSIONS_JS
    assert "await renderSessionList()" in SESSIONS_JS
    assert "_titleRefreshInFlightSids" in SESSIONS_JS
    assert "session_title_already_generating" in SESSIONS_JS


def test_session_title_generation_status_renders_next_to_title():
    assert "session-title-status" in SESSIONS_JS
    assert "session-title-status" in STYLE_CSS
    assert "session_title_generating_short" in SESSIONS_JS
    assert "session_title_generating_short" in I18N_JS


def test_session_context_menu_scrollbar_is_viewport_driven():
    """Default menu should render full height; JS adds scrolling only on viewport overflow."""
    css_rule_start = STYLE_CSS.index(".session-action-menu{")
    css_rule = STYLE_CSS[css_rule_start:STYLE_CSS.index("}", css_rule_start)]
    assert "max-height:" not in css_rule
    assert "overflow-y:auto" not in css_rule
    assert "scrollHeight" in SESSIONS_JS
    assert ".style.maxHeight" in SESSIONS_JS
    assert ".style.overflowY='auto'" in SESSIONS_JS
    assert ".style.overflowY='hidden'" in SESSIONS_JS


def test_force_title_i18n_keys_have_english_fallbacks():
    for key in [
        "session_generate_title",
        "session_generate_title_desc",
        "session_title_generating",
        "session_title_generated",
        "session_title_generate_failed",
        "session_title_already_generating",
        "session_title_generating_short",
    ]:
        assert f"{key}:" in I18N_JS
