"""Regression tests for the 2026-06-11 fork-audit W5 misc UX fixes.

Covers:
- webui-frontend-rest-2: upstream base CSS for sidebar search highlighting.
- webui-frontend-rest-6: session action menu scroll clamp; no duplicate rule.
- webui-frontend-rest-3: artifacts recorded with absolute workspace paths
  open again (workspace prefix is stripped, outside paths stay rejected).
- webui-state-persistence-006: the manual_title 409 branch parses err.body.
- webui-state-persistence-008: loadSession does not await the queue
  reconcile ahead of the first transcript paint.
- webui-frontend-core-6: media/empty live segments are not deduped away.
- webui-api-routes-005: /api/session/reasoning rejects read-only sessions.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


class TestSearchCss:
    def test_upstream_base_rules_restored(self):
        assert ".session-search-hit{" in STYLE_CSS
        assert ".session-search-preview{" in STYLE_CSS
        assert ".session-search-hit-preview{" in STYLE_CSS

    def test_preview_clamped_to_two_lines(self):
        base = STYLE_CSS.split(".session-search-preview{", 1)[1].split("}", 1)[0]
        assert "-webkit-line-clamp:2" in base
        assert "font-size:11px" in base


class TestActionMenuCss:
    # The scroll clamp itself lives in JS (_sessionActionMenu.style.maxHeight,
    # asserted by test_session_touch_actions.py) — CSS only had a stray
    # duplicated .open rule from the merge.
    def test_no_duplicate_open_rule(self):
        assert STYLE_CSS.count(".session-action-menu.open{display:block;}") == 1


class TestLoadSessionFirstPaint:
    def test_reconcile_not_awaited_in_load_session(self):
        # webui-state-persistence-008: the reconcile call must be
        # fire-and-forget (void ...), not awaited before the first render.
        assert "await reconcileSessionQueue(sid)" not in SESSIONS_JS
        assert "void reconcileSessionQueue(sid)" in SESSIONS_JS


class TestForceTitle409Parsing:
    def test_err_body_parsed_as_json(self):
        idx = SESSIONS_JS.index("async function _forceGenerateSessionTitle")
        block = SESSIONS_JS[idx:idx + 4000]
        assert "JSON.parse(_parsedBody)" in block or "JSON.parse(err.body)" in block or "_parsedBody=JSON.parse" in block


_NODE_DRIVER = r"""
const fs = require('fs');
const target = process.argv[2];
const src = fs.readFileSync(target, 'utf8');
const args = JSON.parse(process.argv[3] || '{}');

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', src.indexOf(')', start));
  let depth = 1;
  i++;
  while (depth > 0 && i < src.length) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

if (args.scenario === 'strip-workspace-prefix') {
  var S = {session: {workspace: '/home/user/project/'}};
  const _WORKSPACE_PREVIEW_SECRET_DIR_RE = /$^/;
  const _WORKSPACE_PREVIEW_SECRET_PATH_RE = /$^/;
  eval(extractFunc('_stripWorkspacePrefix'));
  eval(extractFunc('_workspacePreviewRelPath'));
  const inside = _workspacePreviewRelPath(_stripWorkspacePrefix('/home/user/project/src/app.py'));
  const outside = _workspacePreviewRelPath(_stripWorkspacePrefix('/etc/passwd'));
  const relative = _workspacePreviewRelPath(_stripWorkspacePrefix('src/app.py'));
  process.stdout.write(JSON.stringify({inside, outside, relative}));
} else if (args.scenario === 'segment-dedupe') {
  function seg(text, opts = {}) {
    return {
      _text: text,
      _media: !!opts.media,
      _seq: opts.seq || '',
      getAttribute(name) { return name === 'data-live-segment-seq' ? this._seq : null; },
      querySelector(sel) { return this._media ? {} : null; },
    };
  }
  function _liveAssistantSegmentText(s) { return (s && s._text || '').trim(); }
  eval(extractFunc('_liveAssistantSegmentsDuplicate'));
  process.stdout.write(JSON.stringify({
    mediaVsText: _liveAssistantSegmentsDuplicate(seg('', {media: true}), seg('some streamed text')),
    twoMedia: _liveAssistantSegmentsDuplicate(seg('', {media: true}), seg('', {media: true})),
    twoEmptyText: _liveAssistantSegmentsDuplicate(seg(''), seg('')),
    distinctSeqs: _liveAssistantSegmentsDuplicate(seg('abc', {seq: '1'}), seg('abc def', {seq: '2'})),
    staleCopy: _liveAssistantSegmentsDuplicate(seg('abc', {seq: '3'}), seg('abc def', {seq: '3'})),
    identical: _liveAssistantSegmentsDuplicate(seg('same'), seg('same')),
  }));
} else {
  throw new Error('unknown scenario');
}
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("audit_w5_driver") / "driver.js"
    path.write_text(_NODE_DRIVER, encoding="utf-8")
    return str(path)


def _run_node(driver_path, target, scenario):
    result = subprocess.run(
        [NODE, driver_path, str(target), json.dumps({"scenario": scenario})],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_artifact_absolute_path_opens_inside_workspace_only(driver_path):
    got = _run_node(driver_path, REPO_ROOT / "static" / "workspace.js", "strip-workspace-prefix")

    assert got["inside"] == "src/app.py"
    assert got["outside"] == ""  # absolute path outside workspace stays rejected
    assert got["relative"] == "src/app.py"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_live_segment_dedupe_keeps_media_and_distinct_segments(driver_path):
    got = _run_node(driver_path, REPO_ROOT / "static" / "ui.js", "segment-dedupe")

    assert got["mediaVsText"] is False  # streamed image is not a dupe of text
    assert got["twoMedia"] is False  # two media segments are kept
    assert got["twoEmptyText"] is True  # genuinely empty text segments dedupe
    assert got["distinctSeqs"] is False  # different seqs are different segments
    assert got["staleCopy"] is True  # stale shorter copy of same seq dedupes
    assert got["identical"] is True


def test_session_reasoning_route_rejects_read_only_sessions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import api.models as models
    import api.routes as routes
    from api.models import SESSIONS, Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    SESSIONS.clear()

    s = Session(session_id="readonlyreason1", title="RO", messages=[])
    s.read_only = True
    s.save()
    SESSIONS[s.session_id] = s

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": s.session_id, "effort": "high"})
    monkeypatch.setattr(
        routes, "j",
        lambda _handler, payload, status=200, extra_headers=None: captured.update(payload=payload, status=status) or True,
    )
    monkeypatch.setattr(
        routes, "bad",
        lambda _handler, msg, status=400: captured.update(payload={"error": msg}, status=status) or True,
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/reasoning")) is True
    assert captured["status"] == 403
    reloaded = Session.load(s.session_id)
    assert getattr(reloaded, "reasoning_effort", None) in (None, "")
