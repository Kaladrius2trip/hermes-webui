from pathlib import Path
from types import SimpleNamespace

import api.models as models
import api.routes as routes
import api.streaming as streaming
from api.models import SESSIONS, Session


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def _capture_post(monkeypatch, body):
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
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


def _profile_env():
    class _ProfileEnv:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    return _ProfileEnv()


def test_session_menu_exposes_one_canonical_title_action():
    menu_start = SESSIONS_JS.index("function _openSessionActionMenu")
    menu_end = SESSIONS_JS.index("document.addEventListener('click'", menu_start)
    menu_block = SESSIONS_JS[menu_start:menu_end]

    assert "t('session_generate_title')" not in menu_block
    assert "t('session_generate_title_desc')" not in menu_block
    assert menu_block.count("t('session_title_regenerate')") == 1
    assert menu_block.count("t('session_title_regenerate_desc')") == 1
    assert menu_block.count("_forceGenerateSessionTitle(session)") == 1
    assert "api('/api/session/title/regenerate'" not in menu_block


def test_title_refresh_and_regenerate_routes_share_canonical_handler():
    refresh_idx = ROUTES_PY.index('"/api/session/title/refresh"')
    regenerate_idx = ROUTES_PY.index('"/api/session/title/regenerate"')
    next_endpoint_idx = ROUTES_PY.index('"/api/personality/set"', regenerate_idx)
    refresh_block = ROUTES_PY[refresh_idx:regenerate_idx]
    regenerate_block = ROUTES_PY[regenerate_idx:next_endpoint_idx]

    assert "_handle_session_title_refresh(handler, body)" in refresh_block
    # Explicit regenerate forces past the manual-title guard (upstream contract).
    assert "_handle_session_title_refresh(handler, body, force=True)" in regenerate_block
    assert "generate_session_title_for_session" not in regenerate_block
    assert "prefer_latest" not in regenerate_block


def test_balanced_title_context_includes_opening_recent_and_latest_with_role_labels():
    messages = [
        {"role": "user", "content": "Opening goal: improve Hermes WebUI session titles."},
        {"role": "assistant", "content": "We should name the overall conversation."},
        {"role": "user", "content": "Recurring topic: title context should include theme."},
        {"role": "assistant", "content": "Use bounded context, not one exchange."},
        {"role": "user", "content": "Latest narrow status: tests passed."},
        {"role": "assistant", "content": "Done."},
    ]

    user_context, assistant_context = streaming._balanced_title_context_snippets(
        messages,
        recent_exchange_limit=1,
    )

    assert "Opening goal User:" in user_context
    assert "Opening goal Assistant:" in assistant_context
    assert "Recent recurring topic User 1:" in user_context
    assert "Recent recurring topic Assistant 1:" in assistant_context
    assert "Latest exchange User:" in user_context
    assert "Latest exchange Assistant:" in assistant_context
    assert user_context.index("Opening goal User:") < user_context.index("Latest exchange User:")
    assert assistant_context.index("Opening goal Assistant:") < assistant_context.index("Latest exchange Assistant:")
    assert len(user_context) <= 4000
    assert len(assistant_context) <= 4000


def test_generate_session_title_uses_overall_goal_when_latest_is_status_update(monkeypatch):
    session = type("SessionLike", (), {})()
    session.messages = [
        {"role": "user", "content": "Please improve Hermes WebUI session title architecture for long conversations."},
        {"role": "assistant", "content": "I will unify the title generator around conversation-level context."},
        {"role": "user", "content": "Make adaptive refresh preserve the recurring chat theme."},
        {"role": "assistant", "content": "I will keep opening goal, recurring topic, and latest exchange visible."},
        {"role": "user", "content": "Tests passed."},
        {"role": "assistant", "content": "Done."},
    ]

    import api.profiles as profiles_api

    monkeypatch.setattr(profiles_api, "profile_env_for_background_worker", lambda *args, **kwargs: _profile_env())
    captured = {}

    def fake_generate(user_text, assistant_text, agent=None):
        captured["user_text"] = user_text
        captured["assistant_text"] = assistant_text
        return None, "llm_empty", ""

    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", fake_generate)

    title, status, _raw = streaming.generate_session_title_for_session(session)

    assert "Opening goal User:" in captured["user_text"]
    assert "Hermes WebUI session title architecture" in captured["user_text"]
    assert "Latest exchange User: Tests passed" in captured["user_text"]
    assert "Latest exchange Assistant: Done" in captured["assistant_text"]
    assert "passed" not in title.lower()
    assert "Hermes" in title and "WebUI" in title
    assert status == "local_summary:llm_empty"


def test_manual_title_refresh_is_not_overwritten(tmp_path, monkeypatch):
    _isolate_session_store(tmp_path, monkeypatch)
    session = Session(
        session_id="manualtitle1",
        title="Carefully named by user",
        messages=[
            {"role": "user", "content": "Generate a title from this exchange"},
            {"role": "assistant", "content": "A generated title would be possible."},
        ],
        llm_title_generated=False,
    )
    SESSIONS[session.session_id] = session
    session.save()
    captured = _capture_post(monkeypatch, {"session_id": session.session_id})
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", lambda *args, **kwargs: ("Should Not Persist", "llm", ""))

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/title/refresh")) is True

    assert captured["status"] == 409
    assert captured["payload"]["status"] == "manual_title"
    saved = Session.load(session.session_id)
    assert saved is not None
    assert saved.title == "Carefully named by user"
    assert saved.llm_title_generated is False
