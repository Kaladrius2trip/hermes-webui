# Regression tests for _purgeStaleInflightEntries ghost-entry leak (#2092).
#
# When a session is deleted / archived / filtered out of the sidebar list,
# _allSessions no longer contains it.  Previously _purgeStaleInflightEntries()
# only deleted an INFLIGHT entry when the session WAS present and was not
# streaming, leaving ghost entries for absent sessions indefinitely.  The fix
# adds an explicit check: if the sid is absent from _allSessions, the entry is
# always removed.
#
# These are source-level / parse-time regression tests using the same pattern
# as test_inflight_stream_reuse.py.  They verify the function body contains the
# correct guard logic and would break if the fix regresses.
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SESSIONS_JS = (REPO_ROOT / 'static' / 'sessions.js').read_text(encoding='utf-8')


def _function_body(src: str, name: str) -> str:
    marker = f'function {name}('
    start = src.find(marker)
    assert start != -1, f'{name}() not found in sessions.js'
    # Find the opening { of the function body.  After the ')' of the parameter
    # list there may be whitespace (space, newline) before '{'.  We handle both
    # `){` and `) \n{` cases so this works whether or not the source uses a
    # newline between the closing paren and the brace.
    rparen = src.find(')', start)
    assert rparen != -1, f'{name}() closing paren not found'
    brace = src.find('{', rparen)
    assert brace != -1, f'{name}() body brace not found'
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
        i += 1
    assert depth == 0, f'{name}() body did not close'
    return src[brace + 1:i - 1]


def test_purge_removes_entry_when_sid_is_absent_from_all_sessions():
    r'''An INFLIGHT entry whose sid is missing from _allSessions must be removed.

    The original bug: the loop condition was `if (s && !s.is_streaming)`.
    When sid was absent, `sessionsById.get(sid)` returned undefined,
    the `s &&` guard short-circuited, and no deletion occurred.
    The fix adds an explicit `if (!sessionsById.has(sid))` branch before
    the streaming check, so missing sessions are always purged.
    '''
    body = _function_body(SESSIONS_JS, '_purgeStaleInflightEntries')

    # The function must check whether the sid exists in the sessions map.
    assert 'sessionsById.has(sid)' in body, (
        '_purgeStaleInflightEntries() must check sessionsById.has(sid) '
        'to catch sessions absent from _allSessions'
    )

    # There must be a branch that deletes INFLIGHT[sid] for missing sessions.
    # It should appear before the effective streaming check so that missing
    # sessions are always cleaned regardless of their streaming state.
    has_check_pos = body.find('sessionsById.has(sid)')
    assert has_check_pos != -1

    # The deletion for absent sessions must be unconditional (no !s.is_streaming guard).
    # Walk forward from the has() check and verify delete appears without a streaming guard.
    segment = body[has_check_pos:]
    # Find the closing of the outer if block (the next unindented '}' or end of body).
    # Simpler: check the first occurrence of 'delete INFLIGHT[sid]' after has() and
    # verify the intervening code does NOT contain 'is_streaming' before that delete.
    first_delete = segment.find('delete INFLIGHT[sid]')
    assert first_delete != -1, 'No delete INFLIGHT[sid] found after sessionsById.has(sid)'
    between = segment[:first_delete]
    assert 'is_streaming' not in between, (
        'delete INFLIGHT[sid] for absent sessions must not be guarded by is_streaming'
    )


def test_purge_removes_entry_when_sid_present_but_not_effectively_streaming():
    r'''An INFLIGHT entry for a session present in _allSessions with no
    stream/runtime liveness must also be removed (existing behaviour preserved).
    '''
    body = _function_body(SESSIONS_JS, '_purgeStaleInflightEntries')
    assert '_isSessionEffectivelyStreaming(s)' in body, (
        '_purgeStaleInflightEntries() must use the unified stream/runtime '
        'liveness check for sessions present in _allSessions'
    )
    # Verify the delete for the non-live case is present.
    live_pos = body.find('!_isSessionEffectivelyStreaming(s)')
    assert live_pos != -1
    seg = body[live_pos:]
    delete_in_non_live = seg.find('delete INFLIGHT[sid]')
    assert delete_in_non_live != -1, (
        'delete INFLIGHT[sid] must follow the non-live check for sessions without runtime liveness'
    )


def test_purge_preserves_entry_when_sid_present_and_effectively_streaming():
    r'''An INFLIGHT entry for a session present in _allSessions with
    is_streaming/active_stream_id/pending runtime fields must NOT be deleted.
    '''
    body = _function_body(SESSIONS_JS, '_purgeStaleInflightEntries')

    # The non-live branch must be an if without an else that deletes.
    # If an else block deleted on streaming/runtime-live rows, the fix would be wrong.
    # We verify by checking that the body does NOT contain a pattern like:
    # `} else { delete INFLIGHT[sid]; }`  immediately after the liveness check.
    live_pos = body.find('!_isSessionEffectivelyStreaming(s)')
    assert live_pos != -1
    # The delete for non-live rows is in the same if block.
    # We confirm that there is no unconditional delete outside the two guarded paths.
    # Reconstruct the two guarded paths:
    #   1. if (!sessionsById.has(sid)) { delete INFLIGHT[sid]; }
    #   2. if (!_isSessionEffectivelyStreaming(s)) { delete INFLIGHT[sid]; }
    # After both, there should be no third unguarded delete.

    # Count 'delete INFLIGHT[sid]' — there should be exactly 2 (one per guarded path).
    delete_count = body.count('delete INFLIGHT[sid]')
    assert delete_count == 2, (
        f'Expected exactly 2 delete INFLIGHT[sid] statements (one per guarded path), '
        f'found {delete_count}.  Streaming sessions must not be deleted.'
    )