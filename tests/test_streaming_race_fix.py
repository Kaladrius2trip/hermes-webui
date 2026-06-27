"""Tests for #631 — streaming race conditions in messages.js

Bug A: A trailing 'token'/'reasoning' event queued a requestAnimationFrame that
fired after 'done' had already called renderMessages(), causing the thinking card
to reappear below the final answer or the response to render twice.

Bug B: On SSE reconnect, the closure variables (assistantText, reasoningText)
were not reset. Server replays token events into the new EventSource, causing
text to accumulate again from the stale values — response doubled, stuck cursor.

Fixes:
- _streamFinalized flag + _pendingRafHandle stored for cancellation
- done/apperror/cancel: set _streamFinalized, cancel pending rAF, call finalizeThinkingCard
- _scheduleRender: guard on _streamFinalized
- _wireSSE: reset accumulators when (re)opening source, unless stream already finalized
- error handler: bail if _streamFinalized (same as _terminalStateReached)
"""
import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding='utf-8')


class TestStreamFinalized:
    """_streamFinalized flag and rAF cancellation."""

    def test_stream_finalized_declared(self):
        src = read('static/messages.js')
        assert '_streamFinalized' in src, (
            "_streamFinalized must be declared in attachLiveStream"
        )

    def test_pending_raf_handle_declared(self):
        src = read('static/messages.js')
        assert '_pendingRafHandle' in src, (
            "_pendingRafHandle must be declared to enable rAF cancellation"
        )

    def test_schedule_render_guards_on_stream_finalized(self):
        src = read('static/messages.js')
        m = re.search(r'function _scheduleRender\([^)]*\)\{.*?\n  \}', src, re.DOTALL)
        assert m, "_scheduleRender not found"
        fn = m.group(0)
        assert '_streamFinalized' in fn, (
            "_scheduleRender must return early when _streamFinalized is true"
        )

    def test_raf_handle_stored_in_schedule_render(self):
        src = read('static/messages.js')
        assert '_pendingRafHandle=_pendingRafFrameHandle' in src or \
               '_pendingRafHandle = _pendingRafFrameHandle' in src or \
               '_pendingRafHandle=requestAnimationFrame' in src or \
               '_pendingRafHandle = requestAnimationFrame' in src, (
            "rAF handle must be stored in _pendingRafHandle for cancellation"
        )

    def test_done_sets_stream_finalized(self):
        src = read('static/messages.js')
        m = re.search(r"source\.addEventListener\('done'.*?\}\);", src, re.DOTALL)
        assert m, "'done' handler not found"
        fn = m.group(0)
        assert '_streamFinalized=true' in fn or '_streamFinalized = true' in fn, (
            "'done' handler must set _streamFinalized=true"
        )
        assert 'cancelAnimationFrame' in fn, (
            "'done' handler must cancel any pending rAF"
        )
        assert 'finalizeThinkingCard' in fn, (
            "'done' handler must call finalizeThinkingCard() to close thinking card"
        )

    def test_done_sets_stream_finalized_before_fade_window(self):
        """#3195 regression: the 'done' handler must set _streamFinalized=true
        IMMEDIATELY after the early-return guard — before the fade machinery /
        _finishDone() closure runs. Otherwise a stream_end event arriving during
        the fade window sees _streamFinalized=false, calls _restoreSettledSession(),
        and overwrites S.messages with stale server data (assistant text between
        tool-call blocks vanishes on switching back to a settled session).
        """
        src = read('static/messages.js')
        m = re.search(r"source\.addEventListener\('done'.*?\}\);", src, re.DOTALL)
        assert m, "'done' handler not found"
        fn = m.group(0)
        guard_idx = fn.find('if(_streamFinalized) return;')
        assert guard_idx != -1, "'done' handler must early-return on _streamFinalized"
        finalize_idx = fn.find('_streamFinalized=true', guard_idx)
        terminal_idx = fn.find('_terminalStateReached=true', guard_idx)
        assert finalize_idx != -1, "'done' handler must set _streamFinalized=true"
        assert terminal_idx != -1, "'done' handler must set _terminalStateReached"
        assert finalize_idx < terminal_idx, (
            "_streamFinalized=true must be set immediately after the guard "
            "(before _terminalStateReached / fade machinery) — #3195"
        )

    def test_apperror_sets_stream_finalized(self):
        src = read('static/messages.js')
        m = re.search(r"source\.addEventListener\('apperror'.*?\}\);", src, re.DOTALL)
        assert m, "'apperror' handler not found"
        fn = m.group(0)
        assert '_streamFinalized=true' in fn or '_streamFinalized = true' in fn, (
            "'apperror' handler must set _streamFinalized=true"
        )
        assert 'cancelAnimationFrame' in fn

    def test_cancel_sets_stream_finalized(self):
        src = read('static/messages.js')
        m = re.search(r"source\.addEventListener\('cancel'.*?\}\);", src, re.DOTALL)
        assert m, "'cancel' handler not found"
        fn = m.group(0)
        assert '_streamFinalized=true' in fn or '_streamFinalized = true' in fn, (
            "'cancel' handler must set _streamFinalized=true"
        )
        assert 'cancelAnimationFrame' in fn


class TestReconnectAccumulatorPreservation:
    """Bug B regression guard: the accumulators must NOT be reset on reconnect.

    The original PR description claimed the server "replays buffered token
    events" on SSE reconnect, and proposed resetting `assistantText` /
    `reasoningText` inside `_wireSSE` to absorb that replay.  That is not
    how the server actually works — `api/routes._handle_sse_stream` reads
    a one-shot `queue.Queue()` that delivers each event to exactly one
    consumer.  When a client reconnects with the same `stream_id`, it
    picks up from the queue's current position; already-delivered tokens
    are NOT re-sent.  Resetting the accumulators on reconnect would wipe
    the already-displayed content and restart the response from the first
    post-reconnect token — a data-loss regression.

    The "doubled response" / "stuck cursor" symptom that originally
    motivated the reset is fully explained by Bug A (trailing rAF after
    `done` inserting a duplicate live-turn wrapper).  The Bug A fix
    (_streamFinalized guard + cancelAnimationFrame in terminal handlers)
    resolves both symptoms without needing a reset.
    """

    def test_wire_sse_does_not_reset_accumulators(self):
        """Regression guard: the _wireSSE preamble (before any event
        listeners are attached) must not contain a literal accumulator-
        reset statement.  Preserves pre-reconnect content so the user
        sees the full response across a drop+reconnect.

        Turn-boundary resets inside event listeners (tool,
        interim_assistant) are intentional (#2565) and not covered by
        this guard — they prevent reasoning from accumulating across
        multi-turn agent sessions."""
        src = read('static/messages.js')
        m = re.search(r'function _wireSSE\(source\)\{.*?\n  \}', src, re.DOTALL)
        assert m, "_wireSSE not found"
        fn = m.group(0)
        # Check only the preamble before the first addEventListener — this is
        # the reconnect path where resets would cause data loss.
        first_listener = fn.find("source.addEventListener(")
        assert first_listener > 0, "no addEventListener in _wireSSE"
        preamble = fn[:first_listener]
        assert "assistantText=''" not in preamble and 'assistantText = ""' not in preamble, (
            "_wireSSE preamble must NOT reset assistantText — the server does "
            "not replay events on reconnect, so the reset would wipe valid "
            "pre-drop content"
        )
        assert "reasoningText=''" not in preamble and 'reasoningText = ""' not in preamble, (
            "_wireSSE preamble must NOT reset reasoningText on reconnect"
        )

    def test_closure_initialises_accumulators_empty(self):
        """Initial-connect safety: accumulators are initialised to empty at
        the closure scope in attachLiveStream, not inside _wireSSE.  That
        covers the first call; reconnects must preserve whatever was
        accumulated before the drop."""
        src = read('static/messages.js')
        m = re.search(
            r'function attachLiveStream\(.*?function _closeSource',
            src,
            re.DOTALL,
        )
        assert m, "attachLiveStream prelude not found"
        prelude = m.group(0)
        # On initial connect, assistantText and reasoningText are initialised to ''
        # at closure scope (the ternary defaults to '' when reconnecting is false
        # or INFLIGHT has no _live assistant message). On reconnect, they restore
        # from INFLIGHT so the already-rendered content survives the session switch.
        assert ("let assistantText=''" in prelude
                or 'let assistantText = _lastLiveAssistant' in prelude
                or 'let assistantText = ""' in prelude), (
            "assistantText must be initialised at closure scope — "
            "this is the only legitimate reset; _wireSSE must not re-reset"
        )

    def test_error_handler_guards_on_stream_finalized(self):
        """`error` must still bail out when `_streamFinalized` is true —
        otherwise a trailing network 'error' event after `done` would
        attempt a reconnect against a stream that already completed."""
        src = read('static/messages.js')
        m = re.search(r"source\.addEventListener\('error'.*?\}\);", src, re.DOTALL)
        assert m, "'error' handler not found"
        fn = m.group(0)
        assert '_streamFinalized' in fn, (
            "'error' reconnect handler must bail if _streamFinalized is true"
        )

    def test_handle_stream_error_sets_stream_finalized(self):
        """Opus review Q1: _handleStreamError is called after the reconnect fails.
        It calls renderMessages() which settles the DOM. Any pending rAF must be
        cancelled before that renderMessages call — same as done/apperror/cancel."""
        src = read('static/messages.js')
        m = re.search(r'function _handleStreamError\(source\)\{.*?\n  \}', src, re.DOTALL)
        assert m, "_handleStreamError(source) not found"
        fn = m.group(0)
        assert '_streamFinalized=true' in fn or '_streamFinalized = true' in fn, (
            "_handleStreamError must set _streamFinalized=true (Opus Q1 fix)"
        )
        assert 'cancelAnimationFrame' in fn, (
            "_handleStreamError must cancel any pending rAF before renderMessages() runs"
        )

    def test_deferred_stream_recovery_bails_after_session_switch(self):
        """Deferred hidden-tab recovery must not reattach an old stream after
        the user has switched to a different session in the same tab."""
        src = read('static/messages.js')
        m = re.search(r'function _reattachOrRestoreAfterDeferredStreamError\(source\)\{.*?\n  \}', src, re.DOTALL)
        assert m, "_reattachOrRestoreAfterDeferredStreamError(source) not found"
        fn = m.group(0)
        assert 'S.session&&S.session.session_id' in fn
        assert '!==activeSid' in fn
        assert fn.index('!==activeSid') < fn.index('api(`/api/chat/stream/status?stream_id=')


class TestLiveAssistantRestoreReconciliation:
    """Live DOM restore must not preserve duplicate active assistant segments."""

    def test_restore_merge_prunes_duplicate_live_assistant_segments(self):
        """Regression for the observed shape: a restored memory snapshot can
        contain two adjacent live assistant segments with the same prefix, while
        the freshly rendered existing turn has the longer current text.  Merging
        the existing segment into the restored turn must leave exactly one live
        owner for that text; otherwise tokens update the upper copy while the
        lower stale copy keeps the blue cursor.
        """
        import json
        import shutil
        import subprocess

        node = shutil.which('node')
        if node is None:
            import pytest
            pytest.skip('node not on PATH')

        src = read('static/ui.js')

        def extract_func(name, required=True):
            m = re.search(rf'function {name}\(', src)
            if not m:
                if required:
                    raise AssertionError(f'{name} not found')
                return ''
            start = m.start()
            brace = src.index('{', start)
            depth = 0
            for i in range(brace, len(src)):
                if src[i] == '{':
                    depth += 1
                elif src[i] == '}':
                    depth -= 1
                    if depth == 0:
                        return src[start:i + 1]
            raise AssertionError(f'{name} body not found')

        fns = '\n'.join([
            extract_func('_assistantTurnBlocks'),
            extract_func('_liveAssistantSegmentText', required=False),
            extract_func('_liveAssistantSegmentTextLength'),
            extract_func('_liveAssistantSegmentsDuplicate', required=False),
            extract_func('_mergeRestoredLiveAssistantSegment'),
            extract_func('_dedupeLiveAssistantSegments', required=False),
        ])
        driver = r'''
class El {
  constructor(tag='div', attrs={}, classes=[]) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.attrs = {...attrs};
    this.className = classes.join(' ');
    this._text = '';
  }
  appendChild(child){ child.parentElement = this; this.children.push(child); return child; }
  cloneNode(deep=false){
    const copy = new El(this.tagName.toLowerCase(), {...this.attrs}, this.className.split(/\s+/).filter(Boolean));
    copy._text = this._text;
    if(deep) this.children.forEach(ch => copy.appendChild(ch.cloneNode(true)));
    return copy;
  }
  replaceWith(node){
    const parent = this.parentElement;
    const idx = parent.children.indexOf(this);
    if(idx < 0) throw new Error('replace target missing');
    node.parentElement = parent;
    parent.children[idx] = node;
    this.parentElement = null;
  }
  insertAdjacentElement(position, node){
    if(position !== 'afterend') throw new Error('unsupported insert position');
    const parent = this.parentElement;
    const idx = parent.children.indexOf(this);
    node.parentElement = parent;
    parent.children.splice(idx + 1, 0, node);
  }
  remove(){
    if(!this.parentElement) return;
    const idx = this.parentElement.children.indexOf(this);
    if(idx >= 0) this.parentElement.children.splice(idx, 1);
    this.parentElement = null;
  }
  get textContent(){ return this._text + this.children.map(ch => ch.textContent).join(''); }
  set textContent(value){ this._text = String(value || ''); this.children = []; }
  getAttribute(name){ return this.attrs[name]; }
  setAttribute(name, value){ this.attrs[name] = String(value); }
  matches(selector){
    return selector.split(',').some(sel => {
      sel = sel.trim();
      if(sel === '.assistant-turn-blocks') return this.className.split(/\s+/).includes('assistant-turn-blocks');
      if(sel === '.msg-body') return this.className.split(/\s+/).includes('msg-body');
      if(sel === '.tool-card-row') return this.className.split(/\s+/).includes('tool-card-row');
      if(sel === '.tool-call-group') return this.className.split(/\s+/).includes('tool-call-group');
      if(sel === '.agent-activity-thinking') return this.className.split(/\s+/).includes('agent-activity-thinking');
      if(sel === '.thinking-card-row') return this.className.split(/\s+/).includes('thinking-card-row');
      if(sel === '[data-live-assistant="1"]') return this.attrs['data-live-assistant'] === '1';
      return false;
    });
  }
  querySelector(selector){ return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector){
    const out = [];
    const visit = node => {
      for(const child of node.children){
        if(child.matches(selector)) out.push(child);
        visit(child);
      }
    };
    visit(this);
    return out;
  }
}
function segment(text){
  const seg = new El('div', {'data-live-assistant':'1'}, ['assistant-segment']);
  const body = new El('div', {}, ['msg-body']);
  body.textContent = text;
  seg.appendChild(body);
  return seg;
}
function turn(children){
  const row = new El('div', {}, ['msg-row', 'assistant-turn']);
  const blocks = new El('div', {}, ['assistant-turn-blocks']);
  row.appendChild(blocks);
  children.forEach(ch => blocks.appendChild(ch));
  return row;
}
'''
        driver += '\n' + fns + r'''
const partial = 'Ref command hit known pitfall (rev-parse one ref only). Повторю правильно.';
const current = partial + ' Re-running with an explicit branch and workspace.';
const restored = turn([segment(partial), segment(partial)]);
const existing = turn([segment(current)]);
_mergeRestoredLiveAssistantSegment(restored, existing);
const texts = restored.querySelectorAll('[data-live-assistant="1"]').map(seg => seg.querySelector('.msg-body').textContent.trim());
process.stdout.write(JSON.stringify(texts));
'''
        result = subprocess.run([node, '-e', driver], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [
            'Ref command hit known pitfall (rev-parse one ref only). Повторю правильно. Re-running with an explicit branch and workspace.'
        ]
