"""Regression tests for the 2026-06-11 fork-audit W2 streaming fixes.

Covers:
- webui-streaming-004: the English labels of the balanced title context must
  not invert the #3293 language-mismatch guard for short non-Latin chats.
- webui-streaming-006: _record_pending_steer must not insert entries for a
  stream that already finished (permanent STREAM_PENDING_STEERS leak).
"""

import api.streaming as streaming
from api.streaming import (
    _balanced_title_context_snippets,
    _generate_llm_session_title_for_agent,
    _record_pending_steer,
)
from api.config import STREAMS, STREAM_PENDING_STEERS


class TestTitleLanguageGuardWithLabeledContext:
    def _korean_context(self):
        user_text, _ = _balanced_title_context_snippets([
            {"role": "user", "content": "파이썬 오류 수정"},
            {"role": "assistant", "content": "테스트를 실행했습니다"},
            {"role": "user", "content": "파이썬 오류 수정 및 테스트"},
            {"role": "assistant", "content": "수정 완료"},
        ])
        return user_text

    def test_non_latin_title_accepted_despite_english_labels(self, monkeypatch):
        user_text = self._korean_context()
        assert "User" in user_text  # context really is label-decorated
        monkeypatch.setattr(
            streaming,
            "generate_title_raw_via_agent",
            lambda agent, u, a: ("파이썬 오류 수정 및 테스트", "llm_agent"),
        )

        title, status, _ = _generate_llm_session_title_for_agent(object(), user_text, "수정 완료")

        assert status != "llm_language_mismatch"
        assert title == "파이썬 오류 수정 및 테스트"

    def test_latin_title_for_non_latin_chat_still_rejected(self, monkeypatch):
        user_text = self._korean_context()
        monkeypatch.setattr(
            streaming,
            "generate_title_raw_via_agent",
            lambda agent, u, a: ("Python Error Fix Tests", "llm_agent"),
        )

        title, status, _ = _generate_llm_session_title_for_agent(object(), user_text, "수정 완료")

        assert title is None
        assert status == "llm_language_mismatch"


class TestPendingSteerLiveness:
    def test_record_for_dead_stream_is_dropped(self):
        stream_id = "dead-stream-w2-test"
        STREAMS.pop(stream_id, None)
        STREAM_PENDING_STEERS.pop(stream_id, None)

        result = _record_pending_steer(stream_id, "sess-1", "do something else")

        assert result == []
        assert stream_id not in STREAM_PENDING_STEERS

    def test_record_for_live_stream_is_kept(self):
        import queue

        stream_id = "live-stream-w2-test"
        STREAMS[stream_id] = queue.Queue()
        try:
            result = _record_pending_steer(stream_id, "sess-1", "do something else")

            assert len(result) == 1
            assert result[0]["text_preview"] == "do something else"
            assert stream_id in STREAM_PENDING_STEERS
        finally:
            STREAMS.pop(stream_id, None)
            STREAM_PENDING_STEERS.pop(stream_id, None)
