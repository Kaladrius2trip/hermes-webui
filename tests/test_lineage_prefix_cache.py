"""Lineage-prefix cache: the merged snapshot chain is computed once per
nearest-parent id, not on every GET (perf fix for long compression chains)."""

from types import SimpleNamespace

import api.routes as routes


def _sess(sid, parent=None, msgs=None, snapshot=False):
    return SimpleNamespace(
        session_id=sid,
        parent_session_id=parent,
        messages=msgs or [],
        pre_compression_snapshot=snapshot,
        truncation_watermark=None,
    )


def test_chain_loaded_once_then_served_from_cache(monkeypatch):
    routes._LINEAGE_PREFIX_CACHE.clear()
    loads = []
    chain = {
        "p1": _sess("p1", parent="p2", msgs=[{"role": "user", "content": "old1", "timestamp": 1}], snapshot=True),
        "p2": _sess("p2", parent=None, msgs=[{"role": "user", "content": "old0", "timestamp": 0}], snapshot=True),
    }

    def fake_load(sid):
        loads.append(sid)
        return chain.get(sid)

    monkeypatch.setattr(routes.Session, "load", staticmethod(fake_load))
    child = _sess("c", parent="p1", msgs=[{"role": "user", "content": "new", "timestamp": 5}])

    first = routes._webui_sidecar_lineage_messages_for_display(child)
    second = routes._webui_sidecar_lineage_messages_for_display(child)

    assert [m["content"] for m in first] == ["old0", "old1", "new"]
    assert second == first
    assert loads.count("p1") == 1  # chain walked exactly once
    assert loads.count("p2") == 1


def test_no_snapshot_parent_returns_child_messages(monkeypatch):
    routes._LINEAGE_PREFIX_CACHE.clear()
    monkeypatch.setattr(routes.Session, "load", staticmethod(lambda sid: _sess(sid, msgs=[{"role": "user", "content": "x"}], snapshot=False)))
    child = _sess("c", parent="p1", msgs=[{"role": "user", "content": "only"}])

    assert [m["content"] for m in routes._webui_sidecar_lineage_messages_for_display(child)] == ["only"]
