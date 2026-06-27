"""Tests for real /steer functionality (follow-up to PR #1062).

Covers the new POST /api/chat/steer endpoint which mirrors the CLI's /steer
command (cli.py:6140-6155): the endpoint looks up the cached AIAgent for the
session, calls agent.steer(text), and the agent's run loop appends the steer
text to the next tool-result message — no interruption.

Falls back to {"accepted": false, "fallback": "<reason>"} when the agent
isn't running, isn't cached, or doesn't support steer (older agent versions).
The frontend uses the fallback signal to restore the draft without cancelling
the active run.

Plus a leftover-delivery flow: if the agent finishes its turn before the
steer is consumed (no tool-call boundary), _drain_pending_steer is called
after run_conversation returns and a `pending_steer_leftover` SSE event is
emitted so the frontend can queue the leftover text as a next-turn message.
"""
import sys
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture(autouse=True)
def _restore_auth_sessions():
    """Snapshot and restore api.auth._sessions — see test_1058 for the rationale."""
    import api.auth as _auth
    snapshot = dict(_auth._sessions)
    yield
    _auth._sessions.clear()
    _auth._sessions.update(snapshot)


@pytest.fixture
def _clear_caches():
    """Snapshot SESSION_AGENT_CACHE and stream state so tests don't bleed."""
    from api.config import (
        SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK,
        STREAMS, STREAMS_LOCK,
        STREAM_PENDING_STEERS, STREAM_PENDING_STEERS_LOCK,
    )
    with SESSION_AGENT_CACHE_LOCK:
        cache_snap = dict(SESSION_AGENT_CACHE)
        SESSION_AGENT_CACHE.clear()
    with STREAMS_LOCK:
        streams_snap = dict(STREAMS)
        STREAMS.clear()
    with STREAM_PENDING_STEERS_LOCK:
        pending_steers_snap = dict(STREAM_PENDING_STEERS)
        STREAM_PENDING_STEERS.clear()
    yield
    with SESSION_AGENT_CACHE_LOCK:
        SESSION_AGENT_CACHE.clear()
        SESSION_AGENT_CACHE.update(cache_snap)
    with STREAMS_LOCK:
        STREAMS.clear()
        STREAMS.update(streams_snap)
    with STREAM_PENDING_STEERS_LOCK:
        STREAM_PENDING_STEERS.clear()
        STREAM_PENDING_STEERS.update(pending_steers_snap)


def _make_handler():
    """Minimal handler stub matching the methods api.helpers.j() touches."""
    h = MagicMock()
    h.wfile = MagicMock()
    h.headers = MagicMock()
    h.headers.get = MagicMock(return_value="")
    return h


def _captured_response(handler):
    """Pull the JSON body that j() wrote to handler.wfile."""
    import json as _json
    # j() calls handler.wfile.write(body)
    write_calls = handler.wfile.write.call_args_list
    assert write_calls, "no body was written to handler.wfile"
    body = write_calls[-1][0][0]
    return _json.loads(body.decode("utf-8"))


def _captured_status(handler):
    """Pull the HTTP status passed to handler.send_response()."""
    calls = handler.send_response.call_args_list
    assert calls, "no status was sent"
    return calls[-1][0][0]


# ── Backend: the /api/chat/steer endpoint ─────────────────────────────────

class TestHandleChatSteerHappyPath:
    """Endpoint accepts text and calls agent.steer() when all gates pass."""

    def test_accepts_when_agent_cached_and_running(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK, STREAMS, STREAMS_LOCK
        sid, stream_id = "sid_happy", "stream_happy"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        with STREAMS_LOCK:
            import queue as _q
            STREAMS[stream_id] = _q.Queue()

        sess = MagicMock()
        sess.active_stream_id = stream_id
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "Use Python instead"})

        agent.steer.assert_called_once_with("Use Python instead")
        body = _captured_response(handler)
        assert body["accepted"] is True
        assert body["fallback"] is None
        assert body["stream_id"] == stream_id
        assert body["pending_steer_count"] == 1
        assert body["pending_steers"][0]["order"] == 1
        assert body["pending_steers"][0]["text_preview"] == "Use Python instead"

    def test_two_accepted_steers_return_ordered_pending_state(self, _clear_caches):
        """Two /steer calls before a drain stay distinguishable in backend state."""
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK, STREAMS, STREAMS_LOCK
        sid, stream_id = "sid_multi", "stream_multi"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        with STREAMS_LOCK:
            import queue as _q
            STREAMS[stream_id] = _q.Queue()

        sess = MagicMock()
        sess.active_stream_id = stream_id
        with patch("api.streaming.get_session", return_value=sess):
            first = _make_handler()
            _handle_chat_steer(first, {"session_id": sid, "text": "first hint"})
            second = _make_handler()
            _handle_chat_steer(second, {"session_id": sid, "text": "second hint"})

        body1 = _captured_response(first)
        body2 = _captured_response(second)
        assert body1["pending_steer_count"] == 1
        assert [item["text_preview"] for item in body1["pending_steers"]] == ["first hint"]
        assert body2["accepted"] is True
        assert body2["pending_steer_count"] == 2
        assert [item["order"] for item in body2["pending_steers"]] == [1, 2]
        assert [item["text_preview"] for item in body2["pending_steers"]] == ["first hint", "second hint"]
        assert body2["pending_steers"][0]["id"] != body2["pending_steers"][1]["id"]


class TestHandleChatSteerFallbacks:
    """Each gate that fails returns a structured fallback the frontend can branch on."""

    def test_no_cached_agent(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": "sid_x", "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "no_cached_agent"

    def test_agent_lacks_steer_method(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        sid = "sid_old"
        # Older agent without steer() — use spec to suppress MagicMock auto-create
        agent = MagicMock(spec=["interrupt", "run_conversation"])
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "agent_lacks_steer"

    def test_session_not_found(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        sid = "sid_missing"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        with patch("api.streaming.get_session", side_effect=KeyError(sid)):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "session_not_found"
        agent.steer.assert_not_called()  # never reached the steer call

    def test_session_not_running(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        sid = "sid_idle"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        sess = MagicMock()
        sess.active_stream_id = None  # idle session
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "not_running"
        agent.steer.assert_not_called()

    def test_stream_dead(self, _clear_caches):
        """Session has active_stream_id but the stream is gone from STREAMS (e.g. crashed)."""
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        sid = "sid_zombie"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        sess = MagicMock()
        sess.active_stream_id = "stream_zombie"
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "stream_dead"
        agent.steer.assert_not_called()

    def test_steer_raises(self, _clear_caches):
        """If agent.steer() raises, return steer_error rather than 500."""
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK, STREAMS, STREAMS_LOCK
        sid, stream_id = "sid_throws", "stream_throws"
        agent = MagicMock()
        agent.steer = MagicMock(side_effect=RuntimeError("boom"))
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        with STREAMS_LOCK:
            import queue as _q
            STREAMS[stream_id] = _q.Queue()
        sess = MagicMock()
        sess.active_stream_id = stream_id
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "steer_error"


class TestHandleChatSteerInputValidation:
    """Bad input → 400 Bad Request, not silent acceptance."""

    def test_missing_session_id(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"text": "hint"})
        assert _captured_status(handler) == 400

    def test_missing_text(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": "sid"})
        assert _captured_status(handler) == 400

    def test_empty_text_after_strip(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": "sid", "text": "   \n\t  "})
        assert _captured_status(handler) == 400


# ── Routing ───────────────────────────────────────────────────────────────

class TestRouting:
    """The POST handler must dispatch /api/chat/steer to _handle_chat_steer."""

    def test_route_registered(self):
        src = (Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
        assert '/api/chat/steer' in src
        assert '_handle_chat_steer' in src


# ── Frontend: cmdSteer + busy-mode steer use the new endpoint ────────────

class TestFrontendWiring:
    """The slash command and busy-mode steer paths must call /api/chat/steer."""

    @classmethod
    def setup_class(cls):
        cls.cmds = (Path(__file__).parent.parent / "static" / "commands.js").read_text(encoding="utf-8")
        cls.msgs = (Path(__file__).parent.parent / "static" / "messages.js").read_text(encoding="utf-8")
        cls.i18n = (Path(__file__).parent.parent / "static" / "i18n.js").read_text(encoding="utf-8")

    def test_cmd_steer_calls_endpoint(self):
        idx = self.cmds.find("async function cmdSteer(")
        assert idx >= 0
        body = self.cmds[idx:idx + 600]
        # Should call _trySteer (which calls the endpoint), not directly cancelStream
        assert "_trySteer" in body, "cmdSteer must delegate to _trySteer"

    def test_try_steer_calls_endpoint(self):
        idx = self.cmds.find("async function _trySteer(")
        assert idx >= 0
        body = self.cmds[idx:idx + 1500]
        assert "/api/chat/steer" in body, "_trySteer must POST to /api/chat/steer"
        assert "method:'POST'" in body or 'method:"POST"' in body

    def test_try_steer_handles_fallback_without_cancelling(self):
        idx = self.cmds.find("async function _trySteer(")
        body = self.cmds[idx:idx + 1500]
        # Must check result.accepted and surface fallback without queueing or cancelling.
        assert "result&&result.accepted" in body or "result.accepted" in body
        assert "queueSessionMessage" not in body
        assert "cancelStream" not in body, "fallback path must not cancel the stream"
        assert "inp.value" in body, "fallback path must restore the composer draft"

    def test_send_busy_steer_uses_try_steer(self):
        # send() in messages.js: when busyMode === 'steer', should call _trySteer
        idx = self.msgs.find("busyMode==='steer'")
        assert idx >= 0
        block = self.msgs[idx:idx + 800]
        assert "_trySteer" in block, "send()'s steer branch must delegate to _trySteer"

    def test_pending_steer_leftover_listener(self):
        """Frontend must listen for pending_steer_leftover SSE events and queue them."""
        idx = self.msgs.find("addEventListener('pending_steer_leftover'")
        assert idx >= 0, "messages.js must add a listener for pending_steer_leftover"
        block = self.msgs[idx:idx + 1200]
        assert "queueSessionMessage" in block, (
            "pending_steer_leftover handler must queue the leftover text for the next turn"
        )
        assert "clearPendingSteerIndicators" in block, (
            "pending_steer_leftover handler must clear pending steer display once queued"
        )

    def test_pending_steer_indicator_clears_on_apply_and_terminal_paths(self):
        assert "function clearPendingSteerIndicators" in self.cmds
        for marker in (
            "addEventListener('tool_complete'",
            "addEventListener('done'",
            "addEventListener('stream_end'",
            "function _restoreSettledSession",
            "function _handleStreamError",
        ):
            idx = self.msgs.find(marker)
            assert idx >= 0, f"missing frontend stream path: {marker}"
            block = self.msgs[idx:idx + 3600]
            assert "clearPendingSteerIndicators" in block, (
                f"{marker} must clear pending steer display when backend state settles"
            )


# ── i18n keys ─────────────────────────────────────────────────────────────

class TestI18nKeys:
    """The two new keys (cmd_steer_delivered, steer_leftover_queued) must be in all 6 locales."""

    @classmethod
    def setup_class(cls):
        cls.i18n = (Path(__file__).parent.parent / "static" / "i18n.js").read_text(encoding="utf-8")

    def test_cmd_steer_delivered_in_all_locales(self):
        assert self.i18n.count("cmd_steer_delivered:") >= 6, (
            f"cmd_steer_delivered appears {self.i18n.count('cmd_steer_delivered:')} times; "
            f"expected ≥6 (one per locale)"
        )

    def test_steer_leftover_queued_in_all_locales(self):
        assert self.i18n.count("steer_leftover_queued:") >= 6, (
            f"steer_leftover_queued appears {self.i18n.count('steer_leftover_queued:')} times; "
            f"expected ≥6 (one per locale)"
        )


# ── Leftover SSE delivery: streaming.py emits pending_steer_leftover ─────

class TestLeftoverDelivery:
    """After run_conversation returns, leftover /steer metadata is emitted in order."""

    def test_leftover_drain_call_in_streaming(self):
        src = (Path(__file__).parent.parent / "api" / "streaming.py").read_text(encoding="utf-8")
        assert "_emit_pending_steer_leftovers" in src
        assert "_drain_pending_steer" in src
        assert "pending_steer_leftover" in src

    def test_stream_terminal_paths_clear_pending_steer_metadata(self):
        src = (Path(__file__).parent.parent / "api" / "streaming.py").read_text(encoding="utf-8")
        finally_idx = src.find("STREAM_LAST_EVENT_ID.pop(stream_id")
        assert finally_idx >= 0
        finally_block = src[finally_idx:finally_idx + 500]
        assert "_clear_pending_steers(stream_id)" in finally_block
        cancel_idx = src.find("def cancel_stream(stream_id")
        assert cancel_idx >= 0
        clear_idx = src.find("_clear_pending_steers(stream_id)", cancel_idx)
        assert clear_idx > cancel_idx

    def test_leftover_drain_runs_before_done_event(self):
        src = (Path(__file__).parent.parent / "api" / "streaming.py").read_text(encoding="utf-8")
        drain_idx = src.find("_emit_pending_steer_leftovers(")
        assert drain_idx >= 0
        done_idx = src.find("put('done'", drain_idx)
        assert done_idx >= 0
        assert drain_idx < done_idx

    def test_leftover_events_preserve_recorded_steer_order(self, _clear_caches):
        from api import config
        from api.streaming import _record_pending_steer, _emit_pending_steer_leftovers

        sid, stream_id = "sid_leftover_order", "stream_leftover_order"
        import queue as _q
        with config.STREAMS_LOCK:
            config.STREAMS[stream_id] = _q.Queue()
        _record_pending_steer(stream_id, sid, "first hint")
        _record_pending_steer(stream_id, sid, "second hint")

        class Agent:
            def _drain_pending_steer(self):
                return "first hint\nsecond hint"

        events = []
        _emit_pending_steer_leftovers(Agent(), stream_id, sid, lambda event, data: events.append((event, data)))

        assert [event for event, _ in events] == ["pending_steer_leftover", "pending_steer_leftover"]
        assert [data["text"] for _, data in events] == ["first hint", "second hint"]
        assert [data["order"] for _, data in events] == [1, 2]
        assert all(data["source"] == "steer" for _, data in events)
        with config.STREAM_PENDING_STEERS_LOCK:
            assert stream_id not in config.STREAM_PENDING_STEERS

    def test_leftover_emit_uses_agent_drain_when_metadata_diverges(self, _clear_caches):
        from api.streaming import _record_pending_steer, _emit_pending_steer_leftovers

        from api import config
        import queue as _q

        sid, stream_id = "sid_leftover_diverged", "stream_leftover_diverged"
        with config.STREAMS_LOCK:
            config.STREAMS[stream_id] = _q.Queue()
        _record_pending_steer(stream_id, sid, "already applied")
        _record_pending_steer(stream_id, sid, "still pending")

        class Agent:
            def _drain_pending_steer(self):
                return "still pending"

        events = []
        _emit_pending_steer_leftovers(Agent(), stream_id, sid, lambda event, data: events.append((event, data)))
        assert [data["text"] for _, data in events] == ["still pending"]

    def test_leftover_emit_clears_consumed_metadata_without_event(self, _clear_caches):
        from api import config
        from api.streaming import _record_pending_steer, _emit_pending_steer_leftovers

        sid, stream_id = "sid_leftover_consumed", "stream_leftover_consumed"
        _record_pending_steer(stream_id, sid, "already consumed")

        class Agent:
            def _drain_pending_steer(self):
                return None

        events = []
        _emit_pending_steer_leftovers(Agent(), stream_id, sid, lambda event, data: events.append((event, data)))
        assert events == []
        with config.STREAM_PENDING_STEERS_LOCK:
            assert stream_id not in config.STREAM_PENDING_STEERS
