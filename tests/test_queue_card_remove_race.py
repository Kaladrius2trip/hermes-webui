"""Regression: removing a queued item from its card (Steer / Delete) must be
race-proof against a reconcile/echo that still carries the item on the backend.

Repro of the user-reported inconsistency: a message is queued, then steered
from its queue card; the steer fires SSE activity that triggers a queue
reconcile while the optimistic /replace is still in flight. Because the card
buttons removed the item with only `splice + _persistSessionQueue` (no
consumed-tombstone / seq bump, unlike the drain's ackQueuedSessionMessage),
the merge resurrected it — leaving a "steered" item visually queued and
deletable.
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


_DRIVER = r"""
const fs = require('fs');
const ui = fs.readFileSync(process.argv[2], 'utf8');
const args = JSON.parse(process.argv[3] || '{}');

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = ui.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = ui.indexOf('(', start), pd = 1; i++;
  while (pd > 0 && i < ui.length) { const c = ui[i]; if (c === '(') pd++; else if (c === ')') pd--; i++; }
  while (i < ui.length && ui[i] !== '{') i++;
  let d = 1; i++;
  while (d > 0 && i < ui.length) { const c = ui[i]; if (c === '{') d++; else if (c === '}') d--; i++; }
  return ui.slice(start, i);
}

const storage = {};
const sessionStorage = {
  getItem(k){ return Object.prototype.hasOwnProperty.call(storage,k)?storage[k]:null; },
  setItem(k,v){ storage[k]=String(v); }, removeItem(k){ delete storage[k]; },
};
function updateQueueBadge(){}
function showToast(){}
function $(){ return null; }

let backend = [];                       // server-side durable queue
const consumedServer = new Set();       // server tombstones (W3 replace/shift)
function api(url, opts={}) {
  const body = opts.body ? JSON.parse(opts.body) : null;
  if (url.includes('/api/session/queue/replace')) {
    const keep = (body.queue||[]).map(it => _queueItemId(it)).filter(Boolean);
    // server tombstones ids removed by replace (W3)
    backend.forEach(it => { const id=_queueItemId(it); if(id && !keep.includes(id)) consumedServer.add(id); });
    backend = (body.queue||[]).slice();
    return Promise.resolve({ok:true, queue: backend.slice()});
  }
  if (url.includes('/api/session/queue/shift')) {
    if (body && body.item_id) { consumedServer.add(String(body.item_id));
      backend = backend.filter(it => _queueItemId(it) !== String(body.item_id)); }
    return Promise.resolve({ok:true, queue: backend.slice()});
  }
  if (url.includes('/api/session/queue?')) {
    return Promise.resolve({ok:true, queue: backend.slice()});
  }
  if (url.includes('/api/session/queue') && opts.method === 'POST') {
    if (!consumedServer.has(_queueItemId(body.item))) backend.push(body.item);
    return Promise.resolve({ok:true, queue: backend.slice()});
  }
  return Promise.resolve({ok:true});
}

var SESSION_QUEUES={}, _queueMutationSeq={}, _queuePendingIds={}, _queueRenderKeys={};
var _queueConsumedIds={}, _queueConfirmedIds={}, _queueProfileMismatchToasted={};
var _queueConsumedAckInFlight={};
const QUEUE_CONSUMED_TOMBSTONE_MS=10*60*1000;
var S={busy:true, activeProfile:'default', session:{session_id:'s', model:'m'}};

for (const n of ['_getSessionQueue','_queueNextSeq','_queueItemId','_queueMarkPending','_queueClearPending',
  '_queueConsumedHydrate','_queueConsumedPersist','_queuePruneConsumed','_queueMarkConsumed','_queueIsConsumed',
  '_queueMarkConfirmed','_queueIsConfirmed','_queueAckBackend','_queueResyncMissingLocalItem','_queuePreserveLocalFiles',
  '_mergeBackendQueueWithPendingLocal','_applyBackendQueueEcho','_setSessionQueue','reconcileSessionQueue',
  '_persistSessionQueue','queueSessionMessage','_removeQueuedSessionMessage']) {
  try { eval(extractFunc(n)); } catch(e) { /* optional helper */ }
}
async function settle(){ for(let i=0;i<6;i++) await Promise.resolve(); }

(async () => {
  // queue q1 and confirm it durable (echo marks confirmed)
  queueSessionMessage('s', {id:'q1',_queue_id:'q1',text:'do X'});
  await settle();

  if (args.scenario === 'remove-then-reconcile') {
    // Card button removes the item, then a steer-triggered reconcile races in
    // BEFORE we re-read — backend may still echo q1 depending on ordering.
    if (typeof _removeQueuedSessionMessage === 'function') {
      _removeQueuedSessionMessage('s', SESSION_QUEUES['s'][0]);
    } else {
      // current code path: splice + persist (no tombstone)
      const q=_getSessionQueue('s',false); q.splice(0,1); _persistSessionQueue('s',q);
    }
    // Simulate the racing reconcile that the user's steer triggers. Force the
    // backend to still contain q1 (replace hasn't been applied to `backend`
    // from the test's POV yet) to model the worst-case ordering.
    backend = [{id:'q1',_queue_id:'q1',text:'do X'}];
    await reconcileSessionQueue('s');
    await settle();
    process.stdout.write(JSON.stringify({count:(SESSION_QUEUES['s']||[]).length}));
    return;
  }
  process.stdout.write(JSON.stringify({error:'unknown'}));
})().catch(e=>{ console.error(e&&e.stack||e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("queue_remove_race") / "driver.js"
    p.write_text(_DRIVER, encoding="utf-8")
    return str(p)


def _run(driver_path, scenario):
    r = subprocess.run([NODE, driver_path, str(UI_JS_PATH), json.dumps({"scenario": scenario})],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise AssertionError(f"driver failed:\n{r.stdout}\n{r.stderr}")
    return json.loads(r.stdout)


def test_card_remove_survives_racing_reconcile(driver_path):
    got = _run(driver_path, "remove-then-reconcile")
    assert got["count"] == 0, "a card-removed (steered/deleted) item must not be resurrected by a racing reconcile"
