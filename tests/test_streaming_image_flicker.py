"""Streaming image flicker regression.

A MEDIA-bearing live assistant segment re-renders via full innerHTML on every
token tick (the streamed text keeps growing, so the equality guard in
_renderLiveSegmentWithRenderMd never short-circuits). Recreating each <img>
forces an async re-decode that blanks the element for a frame — the reported
"chat flickers in segments while images stream". The fix transplants the
already-decoded <img> nodes (matched by src) into the freshly rendered tree so
their painted state survives the rebuild.
"""

import pathlib

MESSAGES_JS = (pathlib.Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text(encoding="utf-8")


def _fn_body(name):
    start = MESSAGES_JS.index(f"function {name}(")
    depth = 0
    i = MESSAGES_JS.index("{", start)
    j = i
    while j < len(MESSAGES_JS):
        c = MESSAGES_JS[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return MESSAGES_JS[start:j + 1]
        j += 1
    raise AssertionError(f"{name} body not found")


def test_live_rendermd_preserves_images_across_rebuilds():
    body = _fn_body("_renderLiveSegmentWithRenderMd")

    # Captures existing imgs BEFORE the innerHTML rebuild.
    cap = body.index("querySelectorAll('img[src]')")
    rebuild = body.index("assistantBody.innerHTML=renderMd")
    assert cap < rebuild, "must snapshot existing <img> nodes before the innerHTML rebuild"

    # Transplants a cached node into the new tree via replaceChild, matched by src.
    assert "replaceChild(old, im)" in body, "must reuse the decoded <img> node, not the fresh blank one"

    # The transplant runs AFTER sanitisation so it matches sanitised srcs and
    # never resurrects a blocked-scheme image.
    sanitize = body.index("_sanitizeSmdLinks(assistantBody)")
    transplant = body.index("replaceChild(old, im)")
    assert sanitize < transplant, "image transplant must run after link/image sanitisation"

    # Each cached node is reused at most once (no duplicate live node insertion).
    assert "_prevImgs.delete(k)" in body
