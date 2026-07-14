"""Regression tests for the in-chat Todo surface.

The chat surface must use authoritative completed/history tool-result data,
not stale partial tool.start / assistant tool-call args.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"{name} function not found"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} body not found"
    depth = 0
    quote = None
    escaped = False
    i = brace
    while i < len(src):
        ch = src[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        else:
            if ch in ("'", '"', "`"):
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return src[start : i + 1]
        i += 1
    raise AssertionError(f"{name} function body did not close")


def run_node(script: str) -> str:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


def test_chat_todo_surface_markup_lives_inside_chat_context() -> None:
    surface_idx = INDEX_HTML.find('id="chatTodoSurface"')
    assert surface_idx >= 0, "chat Todo surface container must exist"
    shell_idx = INDEX_HTML.rfind('class="messages-shell"', 0, surface_idx)
    messages_idx = INDEX_HTML.find('id="messages"', surface_idx)
    assert shell_idx >= 0, "chat Todo surface must live in .messages-shell"
    assert messages_idx > surface_idx, "chat Todo surface should sit above message history, not in the sidebar Todo tab"
    block = INDEX_HTML[surface_idx - 180 : surface_idx + 260]
    assert 'aria-live="polite"' in block
    assert 'aria-label="Current Todo list"' in block


def test_chat_todo_surface_has_compact_styles() -> None:
    assert ".chat-todo-surface" in STYLE_CSS
    assert ".chat-todo-surface[hidden]" in STYLE_CSS
    assert ".chat-todo-items" in STYLE_CSS
    assert ".chat-todo-open" in STYLE_CSS


def test_latest_todo_state_uses_payload_todos_and_ignores_stale_tool_start_args() -> None:
    funcs = "\n".join(
        [
            extract_function(UI_JS, "_todoToolPayloadTodos"),
            extract_function(UI_JS, "_latestTodoToolState"),
        ]
    )
    script = textwrap.dedent(
        f"""
        {funcs}
        const messages = [
          {{role:'assistant', tool_calls:[{{function:{{name:'todo', arguments:JSON.stringify({{todos:[{{id:'stale', content:'stale partial', status:'in_progress'}}]}})}}}}]}},
          {{role:'tool', content: JSON.stringify({{payload:{{todos:[{{id:'fresh', content:'fresh completed result', status:'pending'}}]}}}})}}
        ];
        const state = _latestTodoToolState(messages);
        if (!state || !state.found) throw new Error('todo state not found');
        if (state.todos.length !== 1 || state.todos[0].id !== 'fresh') {{
          throw new Error('expected authoritative payload.todos from tool role only, got ' + JSON.stringify(state));
        }}
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_latest_empty_payload_is_authoritative_no_active_state() -> None:
    funcs = "\n".join(
        [
            extract_function(UI_JS, "_todoToolPayloadTodos"),
            extract_function(UI_JS, "_latestTodoToolState"),
        ]
    )
    script = textwrap.dedent(
        f"""
        {funcs}
        const messages = [
          {{role:'tool', content: JSON.stringify({{payload:{{todos:[{{id:'old', content:'old', status:'in_progress'}}]}}}})}},
          {{role:'tool', content: JSON.stringify({{payload:{{todos:[]}}}})}}
        ];
        const state = _latestTodoToolState(messages);
        if (!state || !state.found) throw new Error('empty payload.todos should still be an authoritative state');
        if (!Array.isArray(state.todos) || state.todos.length !== 0) {{
          throw new Error('latest empty payload.todos must override older todos: ' + JSON.stringify(state));
        }}
        console.log('ok');
        """
    )
    assert run_node(script).strip() == "ok"


def test_chat_surface_and_sidebar_todo_panel_share_authoritative_parser() -> None:
    assert "function renderChatTodoSurface(" in UI_JS
    assert "_latestTodoToolState" in extract_function(UI_JS, "renderChatTodoSurface")
    load_todos = extract_function(PANELS_JS, "loadTodos")
    assert "_latestTodoToolState" in load_todos
    assert "todoPanel" in load_todos
    # Post-upstream-sync the empty state is centralized in renderTodoEmptyState(),
    # which resolves the same shared todos_no_active key.
    assert "todos_no_active" in load_todos or "renderTodoEmptyState" in load_todos
