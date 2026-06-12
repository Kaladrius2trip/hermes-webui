"""Tests for the chat-adjacent Todos menu (chip + popover).

Todos moved from a separate panel into a composer-anchored popover so
checking progress never leaves the chat. The rail/mobile Todos tabs open
the same menu instead of switching panels.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")


class TestMarkup:
    def test_chip_and_dropdown_present(self):
        assert 'id="composerTodoWrap"' in INDEX_HTML
        assert 'id="composerTodoChip"' in INDEX_HTML
        assert 'id="composerTodoDropdown"' in INDEX_HTML

    def test_rail_tabs_open_menu_not_panel(self):
        assert "openTodoMenuFromRail()" in INDEX_HTML
        assert "switchPanel('todos',{fromRailClick:true})" not in INDEX_HTML

    def test_chat_surface_open_button_opens_popover(self):
        assert 'class="chat-todo-open" onclick="toggleTodoDropdown()"' in UI_JS

    def test_css_uses_underscored_status_class(self):
        assert ".todo-menu-item-status-in_progress" in STYLE_CSS


_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', src.indexOf(')', start));
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    const ch = src[i];
    if (ch === '{') depth++; else if (ch === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

function el(){
  return {style:{},textContent:'',innerHTML:'',hidden:false,
    classList:{_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},toggle(c,v){v?this._s.add(c):this._s.delete(c);},contains(c){return this._s.has(c);}}};
}
const els={composerTodoWrap:el(),composerTodoLabel:el(),composerTodoChip:el(),composerTodoDropdown:el()};
function $(id){return els[id]||null;}
function esc(x){return String(x);}
function t(){return '';}
function closeTodoDropdown(){els.composerTodoDropdown.classList.remove('open');}
function renderTodoDropdown(){}

eval(extractFunc('_todoStatusCounts'));
eval(extractFunc('syncTodoChip'));

// active todos -> count label
syncTodoChip({found:true,todos:[
  {content:'a',status:'completed'},
  {content:'b',status:'in_progress'},
  {content:'c',status:'pending'},
]});
const active={display:els.composerTodoWrap.style.display,label:els.composerTodoLabel.textContent,
  done:els.composerTodoChip.classList.contains('all-done')};

// all done -> checkmark label
syncTodoChip({found:true,todos:[{content:'a',status:'completed'}]});
const done={label:els.composerTodoLabel.textContent,done:els.composerTodoChip.classList.contains('all-done')};

// no todos -> hidden
syncTodoChip({found:false,todos:[]});
const hidden={display:els.composerTodoWrap.style.display};

process.stdout.write(JSON.stringify({active,done,hidden}));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_todo_chip_states(tmp_path):
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    got = json.loads(result.stdout)

    assert got["active"]["display"] == ""
    assert got["active"]["label"] == "1/3"  # 1 of 3 done
    assert got["active"]["done"] is False
    assert got["done"]["label"] == "✓ 1"
    assert got["done"]["done"] is True
    assert got["hidden"]["display"] == "none"
