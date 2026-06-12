"""Backend-owned queued-input store for busy WebUI sessions.

The frontend may keep an optimistic in-memory copy for instant UI feedback, but
this module is the durable source of truth for queued follow-up turns.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from api.config import STATE_DIR
from api.models import is_safe_session_id

QUEUE_DIR = STATE_DIR / "session_queues"
_QUEUE_LOCK = threading.RLock()
_ALLOWED_ITEM_FIELDS = {
    "id",
    "_queue_id",
    "_queued_at",
    "text",
    "files",
    "model",
    "model_provider",
    "profile",
}
_MAX_QUEUE_ITEMS = 200
_MAX_TEXT_CHARS = 200_000
_MAX_META_CHARS = 300
_MAX_FILE_VALUE_CHARS = 2_000
_CONSUMED_QUEUE_IDS: dict[str, dict[str, float]] = {}
_CONSUMED_ID_TTL_SECONDS = 600
_CONSUMED_ID_MAX_PER_SESSION = 512


def _cleanup_consumed_ids_unlocked(sid: str, now: float | None = None) -> dict[str, float]:
    now = time.time() if now is None else now
    items = {
        str(item_id): ts
        for item_id, ts in (_CONSUMED_QUEUE_IDS.get(sid) or {}).items()
        if item_id and now - float(ts or 0) <= _CONSUMED_ID_TTL_SECONDS
    }
    if len(items) > _CONSUMED_ID_MAX_PER_SESSION:
        items = dict(sorted(items.items(), key=lambda kv: kv[1])[-_CONSUMED_ID_MAX_PER_SESSION:])
    if items:
        _CONSUMED_QUEUE_IDS[sid] = items
    else:
        _CONSUMED_QUEUE_IDS.pop(sid, None)
    return items


def _mark_consumed_id_unlocked(sid: str, item_id: str | None) -> None:
    item_id = str(item_id or "").strip()
    if not item_id:
        return
    items = _cleanup_consumed_ids_unlocked(sid)
    items[item_id] = time.time()
    if len(items) > _CONSUMED_ID_MAX_PER_SESSION:
        items = dict(sorted(items.items(), key=lambda kv: kv[1])[-_CONSUMED_ID_MAX_PER_SESSION:])
    _CONSUMED_QUEUE_IDS[sid] = items


def _item_id(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("id") or item.get("_queue_id") or "").strip()


def _queue_path(session_id: str) -> Path:
    sid = str(session_id or "").strip() if isinstance(session_id, str) else ""
    if not is_safe_session_id(sid):
        raise ValueError("invalid session_id")
    return QUEUE_DIR / f"{sid}.json"


def _primitive(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = value if isinstance(value, str) else str(value)
    return text[:_MAX_FILE_VALUE_CHARS]


def _normalize_files(files: Any) -> list[dict[str, Any]]:
    if not isinstance(files, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in files[:50]:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {}
        for key, value in item.items():
            key_s = str(key)
            if not key_s or len(key_s) > 80:
                continue
            if isinstance(value, (dict, list)):
                continue
            entry[key_s] = _primitive(value)
        normalized.append(entry)
    return normalized


def normalize_queue_item(item: dict[str, Any] | None, *, now_ms: int | None = None) -> dict[str, Any]:
    """Return the durable queue-item shape accepted by the backend API."""
    if not isinstance(item, dict):
        item = {}
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    existing_id = str(item.get("id") or item.get("_queue_id") or "").strip()[:_MAX_META_CHARS]
    if not existing_id:
        existing_id = uuid.uuid4().hex
    text = str(item.get("text") or item.get("message") or item.get("content") or "").strip()
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]
    try:
        queued_at = int(item.get("_queued_at") or now)
    except (TypeError, ValueError):
        queued_at = now
    model_provider = item.get("model_provider")
    if model_provider is not None:
        model_provider = str(model_provider)[:_MAX_META_CHARS]
    normalized = {
        "id": existing_id,
        "_queue_id": existing_id,
        "_queued_at": queued_at,
        "text": text,
        "files": _normalize_files(item.get("files")),
        "model": str(item.get("model") or "")[:_MAX_META_CHARS],
        "model_provider": model_provider,
        "profile": str(item.get("profile") or "")[:_MAX_META_CHARS],
    }
    return {key: value for key, value in normalized.items() if key in _ALLOWED_ITEM_FIELDS}


def _read_queue_unlocked(path: Path) -> list[dict[str, Any]]:
    """Read the durable queue. A missing file is an empty queue; a corrupt
    file is moved aside (never silently rewritten by the next mutation); any
    other read error propagates so the route returns 500 and the frontend
    keeps its optimistic copy instead of the mutators destroying the file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        raw = json.loads(text)
    except ValueError:
        try:
            os.replace(path, path.with_name(f"{path.name}.corrupt"))
        except OSError:
            pass
        return []
    if isinstance(raw, dict):
        raw = raw.get("queue")
    if not isinstance(raw, list):
        return []
    return [normalize_queue_item(item) for item in raw if isinstance(item, dict)][: _MAX_QUEUE_ITEMS]


def _write_queue_unlocked(path: Path, queue: list[dict[str, Any]]) -> None:
    queue = [normalize_queue_item(item) for item in queue[: _MAX_QUEUE_ITEMS]]
    if not queue:
        try:
            path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(queue, ensure_ascii=False, separators=(",", ":")))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _payload(session_id: str, queue: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    result = {
        "ok": True,
        "session_id": session_id,
        "queue": list(queue),
        "count": len(queue),
    }
    result.update(extra)
    return result


def list_queue(session_id: str) -> list[dict[str, Any]]:
    path = _queue_path(session_id)
    with _QUEUE_LOCK:
        return _read_queue_unlocked(path)


def append_queue_item(session_id: str, item: dict[str, Any]) -> dict[str, Any]:
    sid = str(session_id).strip()
    path = _queue_path(sid)
    normalized = normalize_queue_item(item)
    normalized_id = _item_id(normalized)
    with _QUEUE_LOCK:
        queue = _read_queue_unlocked(path)
        consumed = _cleanup_consumed_ids_unlocked(sid)
        if normalized_id and normalized_id in consumed:
            # A shift for this optimistic frontend id beat its append request to
            # disk. Treat the late append as already consumed instead of
            # resurrecting a queued turn that the user just sent/removed.
            return _payload(sid, queue, item=None, ignored=True, consumed=True)
        if len(queue) >= _MAX_QUEUE_ITEMS:
            # Reject explicitly instead of truncating the new item away while
            # reporting success — the frontend must keep its copy and tell the
            # user, not silently lose the message.
            return _payload(sid, queue, ok=False, item=None, queue_full=True)
        queue.append(normalized)
        _write_queue_unlocked(path, queue)
        return _payload(sid, queue, item=normalized)


def replace_queue(session_id: str, queue: list[dict[str, Any]] | None) -> dict[str, Any]:
    sid = str(session_id).strip()
    path = _queue_path(sid)
    if queue is None:
        queue = []
    if not isinstance(queue, list):
        raise ValueError("queue must be a list")
    with _QUEUE_LOCK:
        consumed = _cleanup_consumed_ids_unlocked(sid)
        normalized = [
            normalize_queue_item(item)
            for item in queue
            if isinstance(item, dict) and _item_id(normalize_queue_item(item)) not in consumed
        ][: _MAX_QUEUE_ITEMS]
        # Tombstone ids that were on disk but are absent from the replacement
        # (steered/deleted items), mirroring the shift path: a late optimistic
        # append for a removed item must not resurrect an already-handled turn.
        kept_ids = {_item_id(item) for item in normalized}
        for existing in _read_queue_unlocked(path):
            existing_id = _item_id(existing)
            if existing_id and existing_id not in kept_ids:
                _mark_consumed_id_unlocked(sid, existing_id)
        _write_queue_unlocked(path, normalized)
        return _payload(sid, normalized)


def shift_queue_item(session_id: str, item_id: str | None = None) -> dict[str, Any]:
    sid = str(session_id).strip()
    path = _queue_path(sid)
    with _QUEUE_LOCK:
        queue = _read_queue_unlocked(path)
        item = None
        not_found = False
        if queue:
            if item_id:
                item_id = str(item_id).strip()
                for idx, candidate in enumerate(queue):
                    if _item_id(candidate) == item_id:
                        item = queue.pop(idx)
                        break
                if item is None and item_id:
                    # The frontend may shift an optimistic item before its append
                    # request reaches disk. Tombstone the id so that late append
                    # cannot resurrect or duplicate the drained turn.
                    not_found = True
                    _mark_consumed_id_unlocked(sid, item_id)
            else:
                item = queue.pop(0)
        elif item_id:
            not_found = True
            _mark_consumed_id_unlocked(sid, str(item_id).strip())
        if item is not None:
            _mark_consumed_id_unlocked(sid, _item_id(item))
            # Only rewrite the durable file when something was actually
            # removed; a no-op shift must not rewrite (or unlink) state it
            # did not change.
            _write_queue_unlocked(path, queue)
        return _payload(sid, queue, item=item, not_found=not_found)
