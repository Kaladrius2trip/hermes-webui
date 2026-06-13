"""CRUD for multiple LM-Studio-style custom providers (config.yaml round-trip).

The WebUI Providers panel can register several OpenAI-compatible servers, each
with its own base_url (IP) + token. These tests cover the backend helpers in
api/config.py (upsert/remove/list + normalization), the wiring of the
/api/providers/custom endpoints in api/routes.py, and the presence of the
management UI in static/panels.js — without touching the network (probe=False).
"""

import pathlib

import api.config as config


def _redirect_config(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model:\n  provider: anthropic\n", encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    # Let reload_config() run for real against the redirected path so the
    # in-memory cfg (which list_custom_providers reads) reflects each write.
    config.reload_config()
    return cfg_path


def _raw(cfg_path):
    import yaml
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))


def test_upsert_creates_entry_and_preserves_env_literal(monkeypatch, tmp_path):
    cfg_path = _redirect_config(monkeypatch, tmp_path)
    res = config.upsert_custom_provider(
        "LM Studio Local",
        "192.168.1.50:1234",
        api_key="${LMS_TOKEN}",
        models="gpt-oss-20b, qwen2.5",
        probe=False,
    )
    assert res["created"] is True
    assert res["slug"] == "custom:lm-studio-local"
    assert res["base_url"] == "http://192.168.1.50:1234"  # scheme auto-added
    assert res["models"] == ["gpt-oss-20b", "qwen2.5"]

    entry = _raw(cfg_path)["custom_providers"][0]
    # The ${ENV} reference must round-trip as a literal, never the resolved secret.
    assert entry["api_key"] == "${LMS_TOKEN}"
    assert entry["base_url"] == "http://192.168.1.50:1234"
    assert entry["models"] == ["gpt-oss-20b", "qwen2.5"]


def test_upsert_edit_blank_key_preserves_stored_token(monkeypatch, tmp_path):
    cfg_path = _redirect_config(monkeypatch, tmp_path)
    config.upsert_custom_provider("Box", "10.0.0.9:8000", api_key="sk-secret", models=["a"], probe=False)
    # Re-save with an empty key (UI leaves the field blank to keep the secret).
    config.upsert_custom_provider("Box", "10.0.0.9:8000", api_key="", models=["a", "b"], probe=False)
    entries = _raw(cfg_path)["custom_providers"]
    assert len(entries) == 1
    assert entries[0]["api_key"] == "sk-secret"
    assert entries[0]["models"] == ["a", "b"]


def test_upsert_rename_via_original_name(monkeypatch, tmp_path):
    cfg_path = _redirect_config(monkeypatch, tmp_path)
    config.upsert_custom_provider("Old Name", "127.0.0.1:1", api_key="k", models=["m"], probe=False)
    config.upsert_custom_provider("New Name", "127.0.0.1:1", original_name="Old Name", models=["m"], probe=False)
    entries = _raw(cfg_path)["custom_providers"]
    assert len(entries) == 1
    assert entries[0]["name"] == "New Name"


def test_remove_drops_entry_and_clears_empty_list(monkeypatch, tmp_path):
    cfg_path = _redirect_config(monkeypatch, tmp_path)
    config.upsert_custom_provider("Only One", "127.0.0.1:2", probe=False)
    res = config.remove_custom_provider("Only One")
    assert res["removed"] is True
    raw = _raw(cfg_path)
    # The now-empty list is removed rather than left as `custom_providers: []`.
    assert "custom_providers" not in raw


def test_list_custom_providers_never_leaks_secret(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    config.upsert_custom_provider("Lit", "127.0.0.1:3", api_key="sk-plaintext", models=["x"], probe=False)
    config.upsert_custom_provider("Env", "127.0.0.1:4", api_key="${SOME_TOKEN}", probe=False)
    listed = {p["name"]: p for p in config.list_custom_providers()}
    assert "sk-plaintext" not in str(listed)
    assert listed["Lit"]["has_key"] is True
    assert listed["Lit"]["key_is_env"] is False
    assert listed["Env"]["key_is_env"] is True
    assert listed["Env"]["key_env"] == "SOME_TOKEN"


def test_invalid_base_url_scheme_rejected(monkeypatch, tmp_path):
    _redirect_config(monkeypatch, tmp_path)
    import pytest
    with pytest.raises(ValueError):
        config.upsert_custom_provider("Bad", "ftp://x", probe=False)


def test_models_normalization_dedup_and_forms():
    assert config._normalize_custom_provider_models("a, b\nc, a") == ["a", "b", "c"]
    assert config._normalize_custom_provider_models([{"id": "x"}, "y", {"model": "z"}]) == ["x", "y", "z"]
    assert config._normalize_custom_provider_models(None) == []


# ── Endpoint + UI wiring (static assertions, no server boot) ──────────────────

def test_routes_expose_custom_provider_endpoints():
    src = (pathlib.Path("api/routes.py")).read_text(encoding="utf-8")
    assert '/api/providers/custom' in src
    assert '/api/providers/custom/delete' in src
    assert 'upsert_custom_provider' in src
    assert 'remove_custom_provider' in src
    assert 'list_custom_providers' in src


def test_panels_js_has_custom_provider_management():
    src = (pathlib.Path("static/panels.js")).read_text(encoding="utf-8")
    assert "loadCustomProvidersSection" in src
    assert "_saveCustomProvider" in src
    assert "_deleteCustomProvider" in src
    assert "/api/providers/custom" in src
    # Empty token field must NOT be sent so the server preserves the stored key.
    assert "if((opts.api_key||'').trim()) payload.api_key" in src


def test_index_html_has_custom_provider_section():
    src = (pathlib.Path("static/index.html")).read_text(encoding="utf-8")
    assert 'id="customProvidersSection"' in src
    assert 'id="customProvidersList"' in src
    assert 'id="customProviderAddBtn"' in src
