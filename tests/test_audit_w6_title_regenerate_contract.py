"""Regression tests for the 2026-06-11 fork-audit W6 title regenerate contract.

Covers:
- webui-api-routes-002: explicit /api/session/title/regenerate overrides a
  manually renamed session (upstream contract) instead of 409ing with no
  escape hatch; /api/session/title/refresh keeps the manual-title guard.
- webui-state-persistence-007 / api-routes-003: the success path clears
  manual_title (mark_session_title_generated) so adaptive refresh is
  re-enabled once the title is LLM-owned again.
"""

from types import SimpleNamespace

import pytest

import api.models as models
import api.routes as routes
from api.models import SESSIONS, Session


@pytest.fixture
def store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    SESSIONS.clear()
    yield session_dir
    SESSIONS.clear()


def _manual_session(session_id="titlecontract1"):
    s = Session(
        session_id=session_id,
        title="My Hand-Picked Name",
        messages=[
            {"role": "user", "content": "Build a websocket reconnect helper"},
            {"role": "assistant", "content": "Here is the reconnect helper implementation."},
        ],
    )
    s.manual_title = True
    s.llm_title_generated = False
    s.save()
    SESSIONS[s.session_id] = s
    return s


def _capture(monkeypatch, body):
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "_forced_title_context_window", lambda: 1, raising=False)
    monkeypatch.setattr(
        routes, "j",
        lambda _handler, payload, status=200, extra_headers=None: captured.update(payload=payload, status=status) or True,
    )
    monkeypatch.setattr(
        routes, "bad",
        lambda _handler, msg, status=400: captured.update(payload={"error": msg}, status=status) or True,
    )
    return captured


def test_refresh_still_blocks_manual_titles(store, monkeypatch):
    s = _manual_session()
    captured = _capture(monkeypatch, {"session_id": s.session_id})

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/title/refresh")) is True

    assert captured["status"] == 409
    assert captured["payload"]["status"] == "manual_title"
    assert Session.load(s.session_id).title == "My Hand-Picked Name"


def test_regenerate_overrides_manual_title_and_clears_manual_flag(store, monkeypatch):
    s = _manual_session("titlecontract2")
    captured = _capture(monkeypatch, {"session_id": s.session_id})
    monkeypatch.setattr(
        routes,
        "generate_session_title_for_session",
        lambda session, **kwargs: ("Websocket Reconnect Helper", "llm_aux", ""),
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/title/regenerate")) is True

    assert captured["status"] == 200
    assert captured["payload"]["title"] == "Websocket Reconnect Helper"
    saved = Session.load(s.session_id)
    assert saved.title == "Websocket Reconnect Helper"
    assert getattr(saved, "llm_title_generated", False) is True
    assert getattr(saved, "manual_title", True) is False  # adaptive refresh re-enabled


def test_refresh_accepts_explicit_force_flag(store, monkeypatch):
    s = _manual_session("titlecontract3")
    captured = _capture(monkeypatch, {"session_id": s.session_id, "force": True})
    monkeypatch.setattr(
        routes,
        "generate_session_title_for_session",
        lambda session, **kwargs: ("Forced Title", "llm_aux", ""),
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/title/refresh")) is True

    assert captured["status"] == 200
    assert Session.load(s.session_id).title == "Forced Title"
