"""Regression coverage for Edge TTS route safety (#2931)."""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace

import api.auth as auth
import api.routes as routes


class DummyHandler:
    def __init__(self):
        self.status = None
        self.headers_sent = []
        self.wfile = io.BytesIO()
        self.headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers_sent.append((key, value))

    def end_headers(self):
        pass

    @property
    def response_headers(self):
        return dict(self.headers_sent)


def _parsed(query: str):
    return SimpleNamespace(query=query)


def test_tts_requires_auth_before_generation(monkeypatch):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda handler: None)
    monkeypatch.setattr(auth, "verify_session", lambda cookie: False)

    handler = DummyHandler()
    assert routes._handle_tts(handler, _parsed("text=hello")) is True

    assert handler.status == 401
    assert b"Authentication required" in handler.wfile.getvalue()


def test_tts_validates_required_text_and_parameters(monkeypatch):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)

    cases = [
        ("", b"text parameter required"),
        ("text=hello&voice=../bad", b"invalid voice"),
        ("text=hello&rate=999%25", b"invalid rate"),
        ("text=hello&pitch=bad", b"invalid pitch"),
    ]
    for query, expected in cases:
        handler = DummyHandler()
        assert routes._handle_tts(handler, _parsed(query)) is True
        assert handler.status == 400
        assert expected in handler.wfile.getvalue()


def test_tts_returns_503_when_edge_dependency_missing(monkeypatch):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    monkeypatch.setitem(sys.modules, "edge_tts", None)

    handler = DummyHandler()
    assert routes._handle_tts(handler, _parsed("text=hello")) is True

    assert handler.status == 503
    assert b"Edge TTS dependency missing" in handler.wfile.getvalue()


def test_tts_success_uses_mocked_edge_tts(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            self.text = text
            self.voice = voice
            self.kwargs = kwargs

        def save_sync(self, path):
            with open(path, "wb") as f:
                f.write(b"fake-mp3")

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=FakeCommunicate))

    handler = DummyHandler()
    assert routes._handle_tts(
        handler,
        _parsed("text=hello&voice=en-US-AriaNeural&rate=%2B10%25&pitch=%2B5Hz"),
    ) is True

    assert handler.status == 200
    assert handler.response_headers["Content-Type"] == "audio/mpeg"
    assert handler.response_headers["Cache-Control"] == "no-store"
    assert handler.response_headers["X-Content-Type-Options"] == "nosniff"
    assert handler.wfile.getvalue() == b"fake-mp3"
