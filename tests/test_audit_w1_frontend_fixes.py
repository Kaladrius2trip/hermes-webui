"""Regression tests for the 2026-06-11 fork-audit W1 frontend fixes.

Covers:
- webui-frontend-rest-1: _clipSteerPreview restored (steer indicator no longer
  throws ReferenceError on every successful steer).
- webui-state-persistence-001: backend queue echo preserves live File/Blob
  attachments held only in the in-memory queue copy.
- webui-frontend-core-3 / state-persistence-005: reasoning chip refetches when
  the active session changes instead of re-applying the previous session's
  cached effort/override.
- webui-frontend-core-1: every send() path that consumes the composer text
  reports the message as consumed (returns true) so the queue drain acks it.
"""

import json
import pathlib
import shutil
import subprocess

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
COMMANDS_JS_PATH = REPO_ROOT / "static" / "commands.js"
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const args = JSON.parse(process.argv[3] || '{}');

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', src.indexOf(')', start));
  if (src[i] !== '{') throw new Error(name + ' body not found');
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

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

const calls = {api: []};

async function scenarioSteerPreview() {
  eval(extractFunc('_clipSteerPreview'));
  eval(extractFunc('_normalizePendingSteerItems'));
  const long = 'x'.repeat(200);
  const items = _normalizePendingSteerItems([
    {id: 's1', order: 2, text: 'second steer'},
    {id: 's2', order: 1, text: long},
  ], null);
  return {
    count: items.length,
    firstId: items[0].id,
    clippedLen: items.find(i => i.id === 's2').text.length,
    short: _clipSteerPreview('  hello  '),
  };
}

async function scenarioQueueEchoPreservesFiles() {
  var SESSION_QUEUES = {};
  var _queuePendingIds = {};
  var _queueRenderKeys = {};
  var _queueMutationSeq = {};
  const storage = {};
  const sessionStorage = {
    getItem(k) { return Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null; },
    setItem(k, v) { storage[k] = String(v); },
    removeItem(k) { delete storage[k]; },
  };
  function updateQueueBadge() {}
  function showToast() {}
  function api(url, opts) {
    calls.api.push(url);
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    // Echo the appended item back the way the backend does: files normalized
    // to stub dicts (live File objects serialize to {} in JSON).
    if (body && body.item) {
      const stub = {...body.item, files: [{name: 'a.png'}]};
      return Promise.resolve({ok: true, queue: [stub]});
    }
    return Promise.resolve({ok: true, queue: []});
  }
  for (const name of ['_getSessionQueue', '_queueNextSeq', '_queueItemId',
    '_queueMarkPending', '_queueClearPending', '_queuePreserveLocalFiles',
    '_setSessionQueue', 'queueSessionMessage']) {
    eval(extractFunc(name));
  }
  const file = new Blob(['payload'], {type: 'image/png'});
  queueSessionMessage('sess-a', {id: 'q1', _queue_id: 'q1', text: 'with file', files: [file]});
  await settle();
  const q = SESSION_QUEUES['sess-a'] || [];
  return {
    count: q.length,
    fileIsBlob: !!(q[0] && Array.isArray(q[0].files) && q[0].files[0] instanceof Blob),
  };
}

async function scenarioReasoningChipRefetch() {
  var _reasoningChipSid = null;
  var _currentReasoningEffort = null;
  var _currentReasoningSessionOverride = null;
  const applied = [];
  function _normalizeReasoningEffort(v) { return String(v || '').toLowerCase(); }
  function _reasoningEffortQuery() { return ''; }
  function _applyReasoningChip(eff, override) {
    _currentReasoningEffort = eff;
    _currentReasoningSessionOverride = override;
    applied.push({eff, override});
  }
  const efforts = {'sess-a': 'high', 'sess-b': ''};
  function api(url) {
    calls.api.push(url);
    return Promise.resolve({reasoning_effort: 'medium'});
  }
  var S = {session: {session_id: 'sess-a', reasoning_effort: 'high'}};
  eval(extractFunc('fetchReasoningChip'));
  eval(extractFunc('syncReasoningChip'));
  syncReasoningChip();
  await settle();
  const afterFirst = {eff: _currentReasoningEffort, fetches: calls.api.length};
  // Same session again: cached, no refetch.
  syncReasoningChip();
  await settle();
  const afterSame = {fetches: calls.api.length};
  // Switch session: must refetch and show session B's values, not A's override.
  S.session = {session_id: 'sess-b', reasoning_effort: ''};
  syncReasoningChip();
  await settle();
  return {
    afterFirst, afterSame,
    afterSwitchFetches: calls.api.length,
    finalEff: _currentReasoningEffort,
    finalOverride: _currentReasoningSessionOverride,
  };
}

(async () => {
  let result;
  if (args.scenario === 'steer-preview') result = await scenarioSteerPreview();
  else if (args.scenario === 'queue-echo-preserves-files') result = await scenarioQueueEchoPreservesFiles();
  else if (args.scenario === 'reasoning-chip-refetch') result = await scenarioReasoningChipRefetch();
  else throw new Error('unknown scenario ' + args.scenario);
  process.stdout.write(JSON.stringify(result));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("audit_w1_driver") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _run(driver_path, target_js, scenario, **extra):
    assert NODE is not None
    payload = {"scenario": scenario, **extra}
    result = subprocess.run(
        [NODE, driver_path, str(target_js), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"node driver failed for {scenario}:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
        )
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_steer_preview_helper_defined_and_clips(driver_path):
    got = _run(driver_path, COMMANDS_JS_PATH, "steer-preview")

    assert got["count"] == 2
    assert got["firstId"] == "s2"  # order 1 sorts first
    assert got["clippedLen"] == 118  # 117 chars + ellipsis
    assert got["short"] == "hello"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_backend_queue_echo_preserves_live_file_objects(driver_path):
    got = _run(driver_path, UI_JS_PATH, "queue-echo-preserves-files")

    assert got["count"] == 1
    assert got["fileIsBlob"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reasoning_chip_refetches_on_session_switch(driver_path):
    got = _run(driver_path, UI_JS_PATH, "reasoning-chip-refetch")

    assert got["afterFirst"]["fetches"] == 1
    assert got["afterFirst"]["eff"] == "high"  # session override wins
    assert got["afterSame"]["fetches"] == 1  # same session: cache reused
    assert got["afterSwitchFetches"] == 2  # session switch: refetch
    assert got["finalEff"] == "medium"  # profile default, A's override gone
    assert got["finalOverride"] is None


class TestSendConsumedContract:
    """webui-frontend-core-1: paths that consume the composer return true so
    _drainQueuedSessionMessage (which requires accepted===true) acks the item
    instead of re-running it on every turn end."""

    def test_local_command_path_returns_consumed(self):
        assert "hideCmdDropdown();return true;" in MESSAGES_JS
        # All four local-execution paths (local command, cli-only echo,
        # agent-on-webui, plugin) report consumed.
        assert MESSAGES_JS.count("hideCmdDropdown();return true;") >= 4

    def test_local_command_paths_never_return_bare(self):
        assert "hideCmdDropdown();return;" not in MESSAGES_JS

    def test_busy_branch_reports_consumed_when_text_handled(self):
        assert "return !!text;" in MESSAGES_JS
