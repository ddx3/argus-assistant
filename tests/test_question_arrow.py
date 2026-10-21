"""The "Show full question" arrow stands at the center of the
first line of the question (2026-10-08. Requirement: fix the height of the arrow and align it
with the text). Before, the row was aligned by the baseline, and a 22px button was taller
than a 21px line: the arrow was off by 1–2 px from the text. Measured in the browser: the
center offset is now zero, for a one-line question and for an expanded one."""

from __future__ import annotations

from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "static" / "index.html"


def test_the_question_arrow_is_as_tall_as_the_text_line_and_sits_on_the_first_line():
    html = INDEX.read_text(encoding="utf-8")
    assert ".io.question { --q-line:21px; }" in html
    assert ".io.question .q-row { display:flex; align-items:flex-start; gap:8px; }" in html
    assert "line-height:var(--q-line)" in html
    assert ".io.question .q-toggle { flex:none; height:var(--q-line); }" in html
    assert "align-items:baseline; gap:8px; }\n  .io.question p" not in html


def test_an_expanded_question_keeps_its_arrow_so_it_can_be_closed():
    """After a click the arrow vanished at once, and the question could no longer be closed
    (2026-10-08). The feed is rebuilt for every state, and in the markup the arrow was always
    born hidden, while for expanded questions `updateQuestionToggles` showed nothing, so after
    the first update it disappeared. Checked through the real `renderChat`: a click, the next
    state, the arrow is visible and the question is expanded, a second click closes it."""
    html = INDEX.read_text(encoding="utf-8")
    assert 'aria-label="Show full question"${isExpanded ? "" : " hidden"}>' in html
    assert 'aria-label="Show full question" hidden>' not in html


def test_service_reasons_are_not_written_as_the_question_or_next_to_thinking():
    """Requirement (2026-10-08): "asked manually" must not be written in the answer
    block. When there is no question (a hotkey without an utterance before it, a screen shift,
    silence), the server puts the explanation of the gate's decision ("asked manually")
    into `cause` and `reason`. That is not a question: such an answer has no "question"
    block, and the text is not written next to "thinking" or in the line above the utterance
    either. Checked through the real `renderChat`."""
    html = INDEX.read_text(encoding="utf-8")
    assert "const SERVICE_CAUSE = /^(asked manually|\d+ new lines on screen|silence \d+ s)/;" in html
    assert 'const realCause = (a) => (a.cause && !SERVICE_CAUSE.test(a.cause) ? a.cause : "");' in html
    assert "${realCause(a) ? `\n          <div class=\"io question" in html
    # "edit the question" only for a real question; "Ask the same thing again" without it is again a
    # manual request (an empty field)
    assert '${realCause(a) ? `<button type="button" class="iconbtn act edit"' in html
    assert '$("q").value = realCause(a);' in html
    assert "const waitReason = wait && SERVICE_CAUSE.test(wait.reason || \"\")" in html
    assert "${waitCause ? `<div class=\"io output dim\">" in html


def test_the_thinking_dots_sit_on_the_baseline_not_in_the_middle():
    """Requirement (2026-10-08): while it is thinking, the dots must run along the bottom, not
    through the center. The middle dot "·" (U+00B7) is drawn at half the height of the letters;
    the ordinary "." lies on the baseline."""
    html = INDEX.read_text(encoding="utf-8")
    assert '.dots::after { content:"."' in html
    assert '@keyframes dots { 0%{content:"."} 25%{content:".."} 50%{content:"..."} 75%{content:"...."} }' in html
    assert "·" not in html[html.index(".dots::after"):html.index("@keyframes dots")]


def test_there_is_no_bulb_for_deep_thinking_anymore():
    """After five variants of a light bulb, the decision (2026-10-08) was to remove it altogether,
    because its meaning is hard to understand. "Think longer" is not marked in
    any way: neither at "answer" nor next to "thinking", and its dots are
    ordinary."""
    html = INDEX.read_text(encoding="utf-8")
    for gone in ("BULB", "svg.deep", "dots.deep", "dots-deep", "bulb-in", 'class="deep"'):
        assert gone not in html, f"index.html still has something about the bulb: {gone}"
    assert '<span class="badge">thinking<span class="dots"></span></span>' in html
    assert '<div class="tag">answer</div>' in html


def test_display_math_scrolls_sideways_only_never_up_and_down():
    """A short display formula such as E = mc² must not show a scrollbar (2026-10-08). With
    `overflow-x:auto` the second axis also became `auto`, and the superscript stuck out of the box
    by 1 px: a vertical scrollbar appeared on a short formula. Measured: for the formula
    `scrollHeight` equals `clientHeight`; a very long one still scrolls sideways."""
    html = INDEX.read_text(encoding="utf-8")
    assert '.io.output math[display="block"] { display:block; margin:4px 0 7px; padding:2px 0; overflow-x:auto; overflow-y:hidden; }' in html
