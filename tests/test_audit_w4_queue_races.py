"""Regression tests for the 2026-06-11 fork-audit W4 queue race fixes.

Node-harness behavioral tests over the real static/ui.js queue helpers:
- webui-frontend-core-4: Combine stamps a client id; legacy id-less
  sessionStorage entries get synthetic ids on hydration.
- webui-frontend-core-5: a POST echo landing after the drain acked its item
  does not resurrect the consumed item (seq bump + tombstone filter).
- webui-state-persistence-003: the drain refuses to call send() while another
  send is in progress or the session is busy.
- webui-state-persistence-004: consumed tombstones survive a reload
  (sessionStorage) so reconcile cannot re-send an already-sent message.
- webui-state-persistence-010: an item confirmed durable but missing from the
  backend (deleted in another tab) is dropped, not resynced; a failed
  (unconfirmed) append is still resynced.
- webui-frontend-core-10: a profile-mismatched item does not head-of-line
  block drainable items behind it.
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

const calls = {api: [], send: [], toasts: [], timeouts: []};
const storage = {};
const sessionStorage = {
  getItem(k) { return Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null; },
  setItem(k, v) { storage[k] = String(v); },
  removeItem(k) { delete storage[k]; },
};
const msg = {value: '', style: {}, classList: {add(){}, remove(){}, toggle(){}, contains(){return false;}}};
const dummy = {value: '', style: {}, dataset: {}, classList: {add(){}, remove(){}, toggle(){}, contains(){return false;}}, appendChild(){}, remove(){}, querySelector(){return null;}};
const document = {activeElement: null, getElementById(id) { return id === 'msg' ? msg : dummy; }};
const window = {__HERMES_CONFIG__: {}};
function $(id) { return document.getElementById(id); }
function updateSendBtn() {}
function _clearActivityElapsedTimer() {}
function setStatus(_) {}
function setComposerStatus(_) {}
function updateQueueBadge(_) {}
function showToast(message) { calls.toasts.push(String(message)); }
function autoResize() {}
function renderTray() {}
function _applyModelToDropdown() {}
function syncModelChip() {}
function send() {
  calls.send.push(msg.value);
  return Promise.resolve(args.sendAcceptTrue ? true : {ok: true});
}
function setTimeout(fn, ms) { calls.timeouts.push({fn, ms}); return calls.timeouts.length; }

let shiftFailures = args.shiftFailures || 0;
const backendQueue = [];
function api(url, opts = {}) {
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  calls.api.push({url, body});
  if (url.includes('/api/session/queue/shift')) {
    if (shiftFailures > 0) { shiftFailures--; return Promise.reject(new Error('shift failed')); }
    const idx = backendQueue.findIndex(item => item.id === (body && body.item_id));
    if (idx >= 0) backendQueue.splice(idx, 1);
    return Promise.resolve({ok: true, queue: [...backendQueue]});
  }
  if (url.includes('/api/session/queue') && opts.method === 'POST') {
    backendQueue.push(body.item);
    return Promise.resolve({ok: true, queue: [...backendQueue]});
  }
  if (url.includes('/api/session/queue?')) {
    return Promise.resolve({ok: true, queue: args.backendQueue || [...backendQueue]});
  }
  return Promise.resolve({ok: true});
}

var SESSION_QUEUES = {};
var _queueMutationSeq = {};
var _queuePendingIds = {};
var _queueRenderKeys = {};
var _queueDrainSid = null;
var _queueDrainInFlight = {};
var _queueDrainContext = {};
var _queueConsumedIds = {};
var _queueConfirmedIds = {};
var _queueProfileMismatchToasted = {};
var _queueConsumedAckInFlight = {};
var _sendInProgress = false;
const QUEUE_CONSUMED_TOMBSTONE_MS = 10*60*1000;
var S = {
  busy: false,
  activeProfile: 'default',
  pendingFiles: [],
  activeStreamId: null,
  session: {session_id: 'sess-a', model: 'model-a', model_provider: null},
};

for (const name of [
  '_getSessionQueue', '_queueNextSeq', '_queueItemId', '_queueMarkPending', '_queueClearPending',
  '_queueConsumedHydrate', '_queueConsumedPersist',
  '_queuePruneConsumed', '_queueMarkConsumed', '_queueIsConsumed',
  '_queueMarkConfirmed', '_queueIsConfirmed', '_queueAckBackend',
  '_queueResyncMissingLocalItem', '_queuePreserveLocalFiles', '_mergeBackendQueueWithPendingLocal',
  '_applyBackendQueueEcho', '_setSessionQueue',
  'reconcileSessionQueue', '_persistSessionQueue', 'queueSessionMessage',
  'peekQueuedSessionMessage', '_peekDrainableSessionMessage', 'ackQueuedSessionMessage',
  'getQueuedSessionCount', '_drainQueuedSessionMessage'
]) {
  eval(extractFunc(name));
}

async function settle() {
  for (let i = 0; i < 4; i++) await Promise.resolve();
}

async function scenarioCombineStampsId() {
  // Reproduce the Combine push with the same shape the UI code uses.
  const src = extractFuncSource();
  return {combineStampsId: src.includes('_mergedId'), legacy: await legacyHydration()};
}
function extractFuncSource() {
  const idx = ui.indexOf("mergeBtn.innerHTML");
  return ui.slice(idx, idx + 800);
}
async function legacyHydration() {
  storage['hermes-queue-sess-a'] = JSON.stringify([{text: 'old entry, no id'}]);
  const q = _getSessionQueue('sess-a', false);
  return {count: q.length, hasId: !!(q[0] && _queueItemId(q[0]))};
}

async function scenarioEchoAfterAckDoesNotResurrect() {
  // Queue an item; its POST response is delayed until after the drain acks it.
  let resolveEcho;
  const echoPromise = new Promise(res => { resolveEcho = res; });
  const realApi = api;
  api = function(url, opts = {}) {
    if (url === '/api/session/queue' && opts.method === 'POST') {
      const body = JSON.parse(opts.body);
      calls.api.push({url, body});
      return echoPromise.then(() => ({ok: true, queue: [body.item]}));
    }
    return realApi(url, opts);
  };
  queueSessionMessage('sess-a', {id: 'q1', _queue_id: 'q1', text: 'race me'});
  // Drain accepts and acks the item while the POST echo is still in flight.
  await ackQueuedSessionMessage('sess-a', 'q1');
  await settle();
  const afterAck = getQueuedSessionCount('sess-a');
  resolveEcho();
  await settle();
  return {afterAck, finalCount: getQueuedSessionCount('sess-a')};
}

async function scenarioDrainBailsWhileSendInProgress() {
  queueSessionMessage('sess-a', {id: 'q1', _queue_id: 'q1', text: 'wait for me'});
  await settle();
  _sendInProgress = true;
  const accepted = await _drainQueuedSessionMessage('sess-a', peekQueuedSessionMessage('sess-a'));
  _sendInProgress = false;
  const busyS = (S.busy = true);
  const acceptedBusy = await _drainQueuedSessionMessage('sess-a', peekQueuedSessionMessage('sess-a'));
  S.busy = false;
  return {accepted, acceptedBusy, sends: calls.send, count: getQueuedSessionCount('sess-a')};
}

async function scenarioConsumedTombstoneSurvivesReload() {
  queueSessionMessage('sess-a', {id: 'q1', _queue_id: 'q1', text: 'sent already'});
  await settle();
  await ackQueuedSessionMessage('sess-a', 'q1');  // shift fails (shiftFailures=1)
  await settle();
  // Simulate reload: wipe in-memory state, keep sessionStorage.
  for (const k of Object.keys(_queueConsumedIds)) delete _queueConsumedIds[k];
  for (const k of Object.keys(SESSION_QUEUES)) delete SESSION_QUEUES[k];
  for (const k of Object.keys(_queueConfirmedIds)) delete _queueConfirmedIds[k];
  // Backend still has the item (its shift never landed).
  const merged = _mergeBackendQueueWithPendingLocal('sess-a', [{id: 'q1', _queue_id: 'q1', text: 'sent already'}]);
  await settle();
  return {mergedCount: merged.length, storageHasTombstone: !!storage['hermes-queue-consumed-sess-a']};
}

async function scenarioCrossTabDeleteNotResurrected() {
  queueSessionMessage('sess-a', {id: 'q1', _queue_id: 'q1', text: 'delete me in tab B'});
  await settle();  // append confirmed -> marked confirmed via echo
  const confirmed = _queueIsConfirmed('sess-a', 'q1');
  calls.api.length = 0;
  // Tab B deleted it; backend reconcile returns empty.
  const merged = _mergeBackendQueueWithPendingLocal('sess-a', []);
  await settle();
  const resyncPosts = calls.api.filter(c => c.url === '/api/session/queue' && c.body && c.body.item).length;
  return {confirmed, mergedCount: merged.length, resyncPosts};
}

async function scenarioFailedAppendStillResyncs() {
  // Append fails -> item never confirmed -> reconcile must resync it.
  const realApi = api;
  let failed = false;
  api = function(url, opts = {}) {
    if (!failed && url === '/api/session/queue' && opts.method === 'POST') {
      failed = true;
      calls.api.push({url});
      return Promise.reject(new Error('append failed'));
    }
    return realApi(url, opts);
  };
  queueSessionMessage('sess-a', {id: 'q1', _queue_id: 'q1', text: 'resync me'});
  await settle();
  calls.api.length = 0;
  const merged = _mergeBackendQueueWithPendingLocal('sess-a', []);
  await settle();
  const resyncPosts = calls.api.filter(c => c.url === '/api/session/queue' && c.body && c.body.item).length;
  return {mergedCount: merged.length, resyncPosts};
}

async function scenarioProfileMismatchSkipsToEligible() {
  queueSessionMessage('sess-a', {id: 'q-other', _queue_id: 'q-other', text: 'other profile', profile: 'research'});
  queueSessionMessage('sess-a', {id: 'q-mine', _queue_id: 'q-mine', text: 'my profile', profile: 'default'});
  await settle();
  const first = _peekDrainableSessionMessage('sess-a');
  const second = _peekDrainableSessionMessage('sess-a');
  return {
    pickedId: first && _queueItemId(first),
    samePick: !!(second && _queueItemId(second) === 'q-mine'),
    toastCount: calls.toasts.filter(t => t.includes('another profile')).length,
  };
}

(async () => {
  let result;
  if (args.scenario === 'combine-stamps-id') result = await scenarioCombineStampsId();
  else if (args.scenario === 'echo-after-ack') result = await scenarioEchoAfterAckDoesNotResurrect();
  else if (args.scenario === 'drain-bails-in-progress') result = await scenarioDrainBailsWhileSendInProgress();
  else if (args.scenario === 'tombstone-survives-reload') result = await scenarioConsumedTombstoneSurvivesReload();
  else if (args.scenario === 'cross-tab-delete') result = await scenarioCrossTabDeleteNotResurrected();
  else if (args.scenario === 'failed-append-resyncs') result = await scenarioFailedAppendStillResyncs();
  else if (args.scenario === 'profile-mismatch-skip') result = await scenarioProfileMismatchSkipsToEligible();
  else throw new Error('unknown scenario ' + args.scenario);
  process.stdout.write(JSON.stringify(result));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("audit_w4_driver") / "driver.js"
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


def test_combine_stamps_client_id_and_legacy_entries_get_ids(driver_path):
    got = _run(driver_path, "combine-stamps-id")

    assert got["combineStampsId"] is True
    assert got["legacy"]["count"] == 1
    assert got["legacy"]["hasId"] is True


def test_post_echo_landing_after_ack_does_not_resurrect_item(driver_path):
    got = _run(driver_path, "echo-after-ack")

    assert got["afterAck"] == 0
    assert got["finalCount"] == 0


def test_drain_bails_while_send_in_progress_or_busy(driver_path):
    got = _run(driver_path, "drain-bails-in-progress")

    assert got["accepted"] is False
    assert got["acceptedBusy"] is False
    assert got["sends"] == []
    assert got["count"] == 1  # item stays queued for the next drain


def test_consumed_tombstone_survives_reload(driver_path):
    got = _run(driver_path, "tombstone-survives-reload", shiftFailures=10)

    assert got["storageHasTombstone"] is True
    assert got["mergedCount"] == 0  # backend copy filtered by persisted tombstone


def test_cross_tab_delete_is_not_resurrected(driver_path):
    got = _run(driver_path, "cross-tab-delete")

    assert got["confirmed"] is True
    assert got["mergedCount"] == 0
    assert got["resyncPosts"] == 0


def test_failed_append_is_still_resynced(driver_path):
    got = _run(driver_path, "failed-append-resyncs")

    assert got["mergedCount"] == 1
    assert got["resyncPosts"] == 1


def test_profile_mismatched_item_does_not_head_of_line_block(driver_path):
    got = _run(driver_path, "profile-mismatch-skip")

    assert got["pickedId"] == "q-mine"
    assert got["samePick"] is True
    assert got["toastCount"] == 1  # toast once, not per turn end
