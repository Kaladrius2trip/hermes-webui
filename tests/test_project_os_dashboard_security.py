from pathlib import Path
from urllib.parse import urlparse

import pytest


def test_project_os_candidate_roots_are_anchored_and_skip_symlinked_dirs(tmp_path, monkeypatch):
    import api.routes as routes

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".ax").mkdir()
    child = workspace / "child"
    (child / "docs" / "project-os").mkdir(parents=True)

    outside = tmp_path / "outside"
    (outside / ".ax").mkdir(parents=True)
    try:
        (workspace / "linked-outside").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unsupported on this platform")

    monkeypatch.chdir(outside)

    roots = {root.resolve() for root in routes._project_os_candidate_repo_roots(workspace)}

    assert workspace.resolve() in roots
    assert child.resolve() in roots
    assert outside.resolve() not in roots


def test_project_os_dashboard_ignores_active_repo_root_outside_selected_workspace(tmp_path, monkeypatch):
    import api.routes as routes

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    class Handler:
        payload = None

    handler = Handler()

    def fake_trusted(path):
        if path in (None, ""):
            return workspace.resolve()
        return Path(path).expanduser().resolve()

    def fake_workspace_json(repo_root, rel):
        if Path(repo_root).resolve() == workspace.resolve() and rel == ".ax/status/active.json":
            return {"repo_root": str(outside)}
        return None

    def fake_j(_handler, payload, *args, **kwargs):
        _handler.payload = payload
        return True

    monkeypatch.setattr(routes, "get_last_workspace", lambda: str(workspace))
    monkeypatch.setattr(routes, "_project_os_trusted_workspace", fake_trusted)
    monkeypatch.setattr(routes, "_project_os_workspace_json", fake_workspace_json)
    monkeypatch.setattr(routes, "_project_os_workspace_read", lambda repo_root, rel: None)
    monkeypatch.setattr(routes, "git_info_for_workspace", lambda repo_root: {"root": str(repo_root)})
    monkeypatch.setattr(routes, "j", fake_j)

    assert routes._handle_project_os_dashboard(handler, urlparse("/api/project-os/dashboard")) is True

    assert handler.payload is not None
    assert Path(handler.payload["repo_root"]).resolve() == workspace.resolve()
    assert handler.payload["git"] == {"root": str(workspace.resolve())}
