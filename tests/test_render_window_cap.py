"""The done/settled render-window expansion must be capped.

On multi-thousand-message sessions the old paths set the render window to the
FULL renderable count on every turn, forcing a full-transcript DOM rebuild and
freezing the UI ("running … and very slowly updates its state"). The window is
tail-anchored, so a finished turn's Activity is always at the bottom; capping
the expansion keeps it visible without re-rendering ancient history.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def test_done_paths_use_capped_helper_not_full_expansion():
    # Both per-turn settle paths must route through the capped helper, not the
    # old Math.max(current, renderableCount()) full expansion.
    assert MESSAGES_JS.count("_expandRenderWindowForActivity()") >= 2
    assert "_currentMessageRenderWindowSize():50, _messageRenderableMessageCount())" not in MESSAGES_JS


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_expand_helper_caps_large_sessions(tmp_path):
    driver = tmp_path / "d.js"
    driver.write_text(r"""
const fs=require('fs');
const ui=fs.readFileSync(process.argv[2],'utf8');
function extractFunc(name){
  const re=new RegExp('function\\s+'+name+'\\s*\\(');
  const start=ui.search(re); if(start<0) throw new Error(name);
  let i=ui.indexOf('{',ui.indexOf(')',start)),d=1;i++;
  while(d>0&&i<ui.length){const c=ui[i];if(c==='{')d++;else if(c==='}')d--;i++;}
  return ui.slice(start,i);
}
// constants
const MESSAGE_RENDER_WINDOW_DEFAULT=50;
const m=ui.match(/MESSAGE_RENDER_WINDOW_ACTIVITY_MAX=(\d+)/);
const MESSAGE_RENDER_WINDOW_ACTIVITY_MAX=Number(m[1]);
let _renderable=0, _current=0, _messageRenderWindowSize=0;
function _messageRenderableMessageCount(){return _renderable;}
function _currentMessageRenderWindowSize(){return _current;}
eval(extractFunc('_expandRenderWindowForActivity'));
function run(renderable,current){_renderable=renderable;_current=current;_expandRenderWindowForActivity();return _messageRenderWindowSize;}
process.stdout.write(JSON.stringify({
  cap: MESSAGE_RENDER_WINDOW_ACTIVITY_MAX,
  huge: run(6743,50),          // capped
  small: run(120,50),          // full (unchanged behaviour)
  userLoadedMore: run(6743,800)// keep user's larger window
}));
""", encoding="utf-8")
    r = subprocess.run([NODE, str(driver), str(REPO / "static" / "ui.js")],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["huge"] == got["cap"], "huge session must cap at the activity max"
    assert got["small"] == 120, "small session still expands to full"
    assert got["userLoadedMore"] == 800, "must not shrink a window the user already grew"
