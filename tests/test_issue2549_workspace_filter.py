"""Regression tests for issue #2549: per-active-workspace session filter (Slice A)."""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_show_all_workspaces_setting_defaults_true():
    src = read("api/config.py")
    assert '"show_all_workspaces": True' in src, (
        "show_all_workspaces must default to True for backward compat"
    )
    # Belongs to the bool validator set
    assert '"show_all_workspaces"' in src.split("_SETTINGS_BOOL_KEYS")[1].split("}")[0]


def test_sessions_js_applies_workspace_filter_after_project():
    src = read("static/sessions.js")
    partition_idx = src.index("function _partitionSidebarSessionRows")
    body = src[partition_idx:src.index("function renderSessionListFromCache", partition_idx)]
    # The workspace filter now lives in the upstream partition helper, after
    # project checks and before archived/sessionRaw accounting.
    project_idx = body.index("if(_activeProject===NO_PROJECT_FILTER)")
    workspace_idx = body.index("s.workspace!==activeWorkspacePath")
    raw_idx = body.index("sessionsRaw.push(s)")
    assert project_idx < workspace_idx < raw_idx
    assert "function _sidebarWorkspaceFilterState()" in src
    assert "window._showAllWorkspaces!==false" in src
    assert "!s.pinned&&s.workspace" in body


def test_render_workspace_empty_state_uses_partition_returned_state():
    """The empty-state branch must not reference locals scoped inside the partition helper."""
    src = read("static/sessions.js")
    render_idx = src.index("function renderSessionListFromCache")
    render_body = src[render_idx:src.index("function _showProjectPicker", render_idx)]
    assert "!_showAllWs" not in render_body
    assert "_activeWsPath&&sessions.length" not in render_body
    assert "showAllWorkspaces" in render_body
    assert "activeWorkspacePath" in render_body


def test_default_true_means_no_filtering_when_setting_absent():
    """When window._showAllWorkspaces is undefined / true, no rows are dropped."""
    src = read("static/sessions.js")
    # Guard expression: _showAllWs defaults to true (back-compat)
    assert "window._showAllWorkspaces!==false" in src


def test_index_html_has_settings_toggle():
    src = read("static/index.html")
    assert 'id="settingsShowAllWorkspaces"' in src
    assert 'data-i18n="settings_label_all_workspaces"' in src


def test_panels_js_loads_and_saves_setting():
    src = read("static/panels.js")
    assert "settingsShowAllWorkspaces" in src
    assert "show_all_workspaces" in src
    assert "window._showAllWorkspaces=" in src


def test_boot_js_initializes_setting():
    src = read("static/boot.js")
    # Hydrates from server settings and clears on logout
    assert "window._showAllWorkspaces=s.show_all_workspaces!==false" in src
    assert "window._showAllWorkspaces=true" in src


def test_locale_strings_present_in_english():
    src = read("static/i18n.js")
    assert "settings_label_all_workspaces: 'Show sessions from all workspaces'" in src
    assert "settings_desc_all_workspaces:" in src
