"""Tests for the chat-adjacent Todos surface.

Todos live as a passive vertical tab in the right Workspace sidebar; the
composer chip is a live progress indicator that opens that tab. The old
horizontal in-chat card and the popover are retired. Rail/mobile Todos
entries open the same sidebar tab.
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


WORKSPACE_JS = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")


class TestMarkup:
    def test_chip_present_and_opens_sidebar_tab(self):
        assert 'id="composerTodoWrap"' in INDEX_HTML
        assert 'id="composerTodoChip"' in INDEX_HTML
        assert 'onclick="openWorkspaceTodos()"' in INDEX_HTML
        # popover retired
        assert 'composerTodoDropdown' not in INDEX_HTML

    def test_workspace_sidebar_has_todos_tab(self):
        assert 'id="workspaceTodosTab"' in INDEX_HTML
        assert 'id="workspaceTodos"' in INDEX_HTML
        assert 'id="workspaceTodosCount"' in INDEX_HTML
        assert "switchWorkspacePanelTab('todos')" in INDEX_HTML

    def test_tab_switcher_supports_todos(self):
        assert "tab === 'todos'" in WORKSPACE_JS
        assert "renderWorkspaceTodos" in WORKSPACE_JS
        assert "raw==='todos'" in WORKSPACE_JS  # localStorage restore

    def test_rail_tabs_open_sidebar_not_panel(self):
        assert "openTodoMenuFromRail()" in INDEX_HTML
        assert "switchPanel('todos',{fromRailClick:true})" not in INDEX_HTML

    def test_in_chat_horizontal_card_retired(self):
        # renderChatTodoSurface now only feeds the chip/sidebar and keeps the
        # legacy host hidden — no horizontal card markup is produced.
        assert 'class="chat-todo-open"' not in UI_JS
        assert 'host.hidden=true' in UI_JS

    def test_css_rules_for_sidebar_tab(self):
        assert '.rightpanel[data-active-tab="todos"] .workspace-todos{display:flex;}' in STYLE_CSS
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
const els={composerTodoWrap:el(),composerTodoLabel:el(),composerTodoChip:el(),workspaceTodos:el(),workspaceTodosCount:el()};
els.workspaceTodos.hidden=true;
function $(id){return els[id]||null;}
function esc(x){return String(x);}
function t(){return '';}
function renderWorkspaceTodos(){}

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
