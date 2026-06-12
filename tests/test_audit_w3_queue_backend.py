"""Regression tests for the 2026-06-11 fork-audit W3 queue-backend hardening.

Covers:
- webui-queue-001: append at capacity is rejected (queue_full), not silently
  truncated away while reporting success.
- webui-queue-002: read errors no longer convert into destructive writes —
  corrupt files are moved aside, transient OS errors propagate, and a no-op
  shift does not rewrite/unlink the durable file.
- webui-queue-003: replace_queue tombstones removed ids so a late optimistic
  append cannot resurrect a steered/deleted item.
- webui-queue-006: model/profile/model_provider/file values are size-capped.
- webui-queue-007: failed writes do not leak .tmp files.
"""

import json

import pytest

import api.session_queue as session_queue
from api.session_queue import (
    append_queue_item,
    list_queue,
    normalize_queue_item,
    replace_queue,
    shift_queue_item,
)


@pytest.fixture(autouse=True)
def _isolated_queue_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(session_queue, "QUEUE_DIR", tmp_path)
    session_queue._CONSUMED_QUEUE_IDS.clear()
    yield
    session_queue._CONSUMED_QUEUE_IDS.clear()


SID = "queuew3session1"


def test_append_at_capacity_returns_queue_full_and_keeps_existing_items(monkeypatch):
    monkeypatch.setattr(session_queue, "_MAX_QUEUE_ITEMS", 3)
    for i in range(3):
        append_queue_item(SID, {"id": f"q{i}", "text": f"msg {i}"})

    result = append_queue_item(SID, {"id": "q-overflow", "text": "one too many"})

    assert result["ok"] is False
    assert result["queue_full"] is True
    assert result["item"] is None
    persisted = list_queue(SID)
    assert [item["id"] for item in persisted] == ["q0", "q1", "q2"]


def test_corrupt_queue_file_is_moved_aside_not_rewritten(tmp_path):
    path = session_queue._queue_path(SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert list_queue(SID) == []
    corrupt = path.with_name(f"{path.name}.corrupt")
    assert corrupt.exists()
    assert corrupt.read_text(encoding="utf-8") == "{not json"
    assert not path.exists()


def test_transient_read_error_propagates_instead_of_emptying_queue(monkeypatch):
    append_queue_item(SID, {"id": "q1", "text": "keep me"})
    path = session_queue._queue_path(SID)
    real_read_text = type(path).read_text

    def flaky_read_text(self, *args, **kwargs):
        if self.name == path.name:
            raise OSError("sharing violation")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", flaky_read_text)
    with pytest.raises(OSError):
        append_queue_item(SID, {"id": "q2", "text": "new"})
    monkeypatch.setattr(type(path), "read_text", real_read_text)

    assert [item["id"] for item in list_queue(SID)] == ["q1"]


def test_noop_shift_does_not_rewrite_or_unlink_queue_file():
    append_queue_item(SID, {"id": "q1", "text": "stays"})
    path = session_queue._queue_path(SID)
    before = path.read_text(encoding="utf-8")

    result = shift_queue_item(SID, "missing-id")

    assert result["item"] is None
    assert result["not_found"] is True
    assert path.exists()
    assert path.read_text(encoding="utf-8") == before


def test_replace_tombstones_removed_ids_against_late_append():
    append_queue_item(SID, {"id": "q-steered", "text": "steer me"})
    append_queue_item(SID, {"id": "q-keep", "text": "keep me"})

    # Frontend steers q-steered out of the queue via replace.
    replace_queue(SID, [{"id": "q-keep", "text": "keep me"}])
    # The item's original optimistic append lands late.
    late = append_queue_item(SID, {"id": "q-steered", "text": "steer me"})

    assert late.get("ignored") is True
    assert late.get("consumed") is True
    assert [item["id"] for item in list_queue(SID)] == ["q-keep"]


def test_metadata_fields_are_size_capped():
    item = normalize_queue_item({
        "id": "x" * 10_000,
        "text": "hello",
        "model": "m" * 10_000,
        "model_provider": "p" * 10_000,
        "profile": "f" * 10_000,
        "files": [{"name": "v" * 100_000}],
    })

    cap = session_queue._MAX_META_CHARS
    assert len(item["id"]) == cap
    assert len(item["model"]) == cap
    assert len(item["model_provider"]) == cap
    assert len(item["profile"]) == cap
    assert len(item["files"][0]["name"]) == session_queue._MAX_FILE_VALUE_CHARS


def test_failed_write_cleans_up_tmp_file(monkeypatch):
    path = session_queue._queue_path(SID)

    real_replace = session_queue.os.replace

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(session_queue.os, "replace", failing_replace)
    with pytest.raises(OSError):
        append_queue_item(SID, {"id": "q1", "text": "msg"})
    monkeypatch.setattr(session_queue.os, "replace", real_replace)

    leftovers = list(path.parent.glob(f"{path.name}.tmp.*"))
    assert leftovers == []


def test_written_queue_file_round_trips():
    append_queue_item(SID, {"id": "q1", "text": "msg", "files": [{"name": "a.png"}]})
    path = session_queue._queue_path(SID)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert isinstance(raw, list)
    assert raw[0]["id"] == "q1"
    assert list_queue(SID)[0]["text"] == "msg"
