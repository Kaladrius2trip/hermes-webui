"""Regression tests for WebUI queued-message data loss.

The queue is backend-canonical, but the browser keeps an optimistic cache.  A
queued turn must never be removed from every durable/visible place until the
next send is accepted.  These tests execute the real queue helpers from
static/ui.js in a tiny Node harness instead of only grepping source strings.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
const args = JSON.parse(process.argv[3] || '{}');

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('(', start);
  let parenDepth = 1;
  i++;
  while (parenDepth > 0 && i < ui.length) {
    const ch = ui[i];
    if (ch === '(') parenDepth++;
    else if (ch === ')') parenDepth--;
    i++;
  }
  while (i < ui.length && ui[i] !== '{') i++;
  if (ui[i] !== '{') throw new Error(name + ' body not found');
  let depth = 1;
  i++;
  while (depth > 0 && i < ui.length) {
    const ch = ui[i];
    if (ch === '{') depth++;
    else if (ch === '}') depth--;
    i++;
  }
  return ui.slice(start, i);
}

const calls = {api: [], send: [], toasts: [], badges: [], timeouts: []};
const storage = {};
const sessionStorage = {
  getItem(k) { return Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null; },
  setItem(k, v) { storage[k] = String(v); },
  removeItem(k) { delete storage[k]; },
};
const msg = {value: '', style: {}, classList: {add(){}, remove(){}, toggle(){}, contains(){return false;}}};
const dummy = {value: '', style: {}, dataset: {}, classList: {add(){}, remove(){}, toggle(){}, contains(){return false;}}, appendChild(){}, remove(){}, querySelector(){return null;}};
const document = {
  activeElement: null,
  getElementById(id) { return id === 'msg' ? msg : dummy; },
};
const window = {__HERMES_CONFIG__: {}};
function $(id) { return document.getElementById(id); }
function updateSendBtn() {}
function _clearActivityElapsedTimer() {}
function setStatus(_) {}
function setComposerStatus(_) {}
function updateQueueBadge(sid) { calls.badges.push(sid); }
function showToast(message) { calls.toasts.push(String(message)); }
function autoResize() {}
function renderTray() {}
function _applyModelToDropdown() {}
function syncModelChip() {}
function send() {
  calls.send.push(msg.value);
  if (args.sendReject) return Promise.reject(new Error('send failed'));
  return Promise.resolve(args.sendAcceptTrue ? true : {ok: true});
}
function setTimeout(fn, ms) { calls.timeouts.push({fn, ms}); return calls.timeouts.length; }

let appendFailuresLeft = args.appendRejectOnce ? 1 : 0;
let shiftFailuresLeft = args.shiftRejectOnce ? 1 : 0;
function api(url, opts = {}) {
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  calls.api.push({url, body});
  if (url.includes('/api/session/queue') && !url.includes('/shift') && opts.method === 'POST' && appendFailuresLeft > 0) {
    appendFailuresLeft--;
    return Promise.reject(new Error('append failed'));
  }
  if (url.includes('/api/session/queue/shift')) {
    if (shiftFailuresLeft > 0) {
      shiftFailuresLeft--;
      return Promise.reject(new Error('shift failed'));
    }
    return Promise.resolve({ok: true, queue: [], item: body && body.item_id ? {id: body.item_id, _queue_id: body.item_id, text: 'shifted'} : null});
  }
  if (url.includes('/api/session/queue') && opts.method === 'POST') {
    return Promise.resolve({ok: true, queue: [body.item], item: body.item});
  }
  if (url.includes('/api/session/queue?')) {
    return Promise.resolve({ok: true, queue: []});
  }
  return Promise.resolve({ok: true});
}

var SESSION_QUEUES = {};
var _queueMutationSeq = {};
var _queuePendingIds = {};
var _queueRenderKeys = {};
var _queueCollapsed = {};
var _queueRenderedSid = null;
var _queueDrainSid = null;
var _queueDrainInFlight = {};
var _queueDrainContext = {};
var _queueConsumedIds = {};
var _queueConsumedAckInFlight = {};
const QUEUE_CONSUMED_TOMBSTONE_MS = 10*60*1000;
var S = {
  busy: true,
  activeProfile: 'default',
  pendingFiles: [],
  activeStreamId: null,
  session: {session_id: 'sess-a', model: 'model-a', model_provider: null},
};

for (const name of [
  '_getSessionQueue', '_queueNextSeq', '_queueItemId', '_queueMarkPending', '_queueClearPending',
  '_queuePruneConsumed', '_queueMarkConsumed', '_queueIsConsumed', '_queueAckBackend',
  '_queueResyncMissingLocalItem', '_mergeBackendQueueWithPendingLocal', '_setSessionQueue',
  'reconcileSessionQueue', '_persistSessionQueue', 'queueSessionMessage',
  'peekQueuedSessionMessage', 'ackQueuedSessionMessage', 'shiftQueuedSessionMessage',
  'getQueuedSessionCount', '_drainQueuedSessionMessage', 'setBusy'
]) {
  eval(extractFunc(name));
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

async function scenarioKeepsItemUntilDrainCallback() {
  queueSessionMessage('sess-a', {id: 'q1', _queue_id: 'q1', text: 'keep me', model: 'm'});
  await settle();
  calls.api.length = 0;
  S.busy = true;
  setBusy(false);
  const countAfterSetBusy = getQueuedSessionCount('sess-a');
  S.session = {session_id: 'other', model: 'm'};
  if (!calls.timeouts.length) throw new Error('drain did not schedule timeout');
  await calls.timeouts[0].fn();
  await settle();
  return {countAfterSetBusy, finalCount: getQueuedSessionCount('sess-a'), sends: calls.send, api: calls.api, storage};
}

async function scenarioSendFailureKeepsQueuedItem() {
  queueSessionMessage('sess-a', {id: 'q2', _queue_id: 'q2', text: 'send fails', model: 'm'});
  await settle();
  calls.api.length = 0;
  S.busy = true;
  setBusy(false);
  if (!calls.timeouts.length) throw new Error('drain did not schedule timeout');
  await calls.timeouts[0].fn();
  await settle();
  return {count: getQueuedSessionCount('sess-a'), text: _getSessionQueue('sess-a', false)[0] && _getSessionQueue('sess-a', false)[0].text, sends: calls.send, api: calls.api, storage};
}

async function scenarioAcceptedSendAckFailureDoesNotResurface() {
  queueSessionMessage('sess-a', {id: 'q5', _queue_id: 'q5', text: 'sent once', model: 'm', profile: 'default'});
  await settle();
  calls.api.length = 0;
  S.busy = true;
  setBusy(false);
  if (!calls.timeouts.length) throw new Error('drain did not schedule timeout');
  await calls.timeouts[0].fn();
  await settle();
  const afterDrainCount = getQueuedSessionCount('sess-a');
  const consumedAfterDrain = !!(_queueConsumedIds['sess-a'] && _queueConsumedIds['sess-a']['q5']);
  _mergeBackendQueueWithPendingLocal('sess-a', [{id: 'q5', _queue_id: 'q5', text: 'sent once'}]);
  await settle();
  return {afterDrainCount, finalCount: getQueuedSessionCount('sess-a'), consumedAfterDrain, sends: calls.send, api: calls.api};
}

async function scenarioAppendFailureBecomesResyncable() {
  queueSessionMessage('sess-a', {id: 'q3', _queue_id: 'q3', text: 'resync me', model: 'm'});
  await settle();
  const apiAfterInitialFailure = calls.api.length;
  _mergeBackendQueueWithPendingLocal('sess-a', []);
  await settle();
  return {apiAfterInitialFailure, totalApi: calls.api.length, count: getQueuedSessionCount('sess-a'), pending: _queuePendingIds['sess-a'] || null};
}

async function scenarioQueueInvalidatesRenderKey() {
  _queueRenderKeys['sess-a'] = 'stale-key';
  queueSessionMessage('sess-a', {id: 'q4', _queue_id: 'q4', text: 'render me', model: 'm'});
  await settle();
  return {hasRenderKey: Object.prototype.hasOwnProperty.call(_queueRenderKeys, 'sess-a'), count: getQueuedSessionCount('sess-a')};
}

(async () => {
  let result;
  if (args.scenario === 'keeps-item-until-drain-callback') result = await scenarioKeepsItemUntilDrainCallback();
  else if (args.scenario === 'send-failure-keeps-item') result = await scenarioSendFailureKeepsQueuedItem();
  else if (args.scenario === 'accepted-send-ack-failure-no-resurface') result = await scenarioAcceptedSendAckFailureDoesNotResurface();
  else if (args.scenario === 'append-failure-resyncable') result = await scenarioAppendFailureBecomesResyncable();
  else if (args.scenario === 'queue-invalidates-render-key') result = await scenarioQueueInvalidatesRenderKey();
  else throw new Error('unknown scenario ' + args.scenario);
  process.stdout.write(JSON.stringify(result));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("queue_drain_driver") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _run(driver_path, scenario, **extra):
    assert NODE is not None
    payload = {"scenario": scenario, **extra}
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(f"node driver failed for {scenario}:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout)


def test_queue_drain_does_not_remove_item_before_send_or_on_session_switch(driver_path):
    got = _run(driver_path, "keeps-item-until-drain-callback")

    assert got["countAfterSetBusy"] == 1
    assert got["finalCount"] == 1
    assert got["sends"] == []


def test_queue_drain_send_failure_keeps_the_queued_item(driver_path):
    got = _run(driver_path, "send-failure-keeps-item", sendReject=True)

    assert got["count"] == 1
    assert got["text"] == "send fails"
    assert got["sends"] == ["send fails"]


def test_queue_drain_ack_failure_does_not_resurface_or_resend_accepted_item(driver_path):
    got = _run(
        driver_path,
        "accepted-send-ack-failure-no-resurface",
        sendAcceptTrue=True,
        shiftRejectOnce=True,
    )

    assert got["sends"] == ["sent once"]
    assert got["afterDrainCount"] == 0
    assert got["consumedAfterDrain"] is True
    assert got["finalCount"] == 0


def test_failed_backend_append_clears_pending_flag_so_reconcile_resyncs(driver_path):
    got = _run(driver_path, "append-failure-resyncable", appendRejectOnce=True)

    assert got["apiAfterInitialFailure"] == 1
    assert got["totalApi"] >= 2
    assert got["count"] == 1
    assert got["pending"] in ({}, None)


def test_optimistic_queue_append_invalidates_render_fingerprint(driver_path):
    got = _run(driver_path, "queue-invalidates-render-key")

    assert got["count"] == 1
    assert got["hasRenderKey"] is False
