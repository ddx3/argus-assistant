"""The frame that went to the model can be opened from the page, both from the answer card
and from the conversation feed, without leaving the app.

The page is one HTML file with JS inside, so a real browser is not needed in the tests:
we check the structure by text, like the neighboring `test_app.py` tests, and run the
pairing of a frame with an answer through `node`, using the same function that sits in
the page.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from argus.ui.server import STATIC


def _page() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _js_block(html: str, marker: str) -> str:
    """The source from `marker` to the matching closing brace of the first `{` after it."""
    start = html.index(marker)
    depth = 0
    for i in range(html.index("{", start), len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
    raise AssertionError(f"block {marker!r} has no closing brace")


def _js_function(html: str, name: str) -> str:
    return _js_block(html, f"function {name}(")


def test_the_answer_card_can_open_the_frame_it_went_with():
    """The frame is stored (`app.state.shots`, `/shot/<id>.jpg`), but before it was visible only
    in the conversation feed; from the answer card, where the frame is mentioned, it could
    not be reached.

    The label is a button inside `.actions`, like "Again" and "Edit",
    and the shared handler of the answer feed treats any button in `.actions` as "again".
    Pressing the label has to be handled first: otherwise a click on it asks the question
    again, and that is a call to the model and money.
    """
    html = _page()

    assert 'class="withshot shotlink"' in html
    assert 'id="shotview"' in html
    assert "function answerShots" in html

    handler = _js_block(html, '$("chat").addEventListener("click"')
    assert handler.index('closest(".shotlink")') < handler.index('closest(".actions button")'), (
        "a click on the frame label would reach \"Again\" and ask the question anew"
    )
    assert handler.index('closest(".shotpeek img")') < handler.index('closest(".actions button")')


def test_the_label_slides_the_frame_out_and_the_frame_itself_opens_full_size():
    """The label "with screenshot" slides the frame out of the card rather
    than opening it at once; the frame opens full size (`showShot`) with a click on the frame
    itself.

    The "slid out" state lives in `shotOpen`, not in the markup: the feed cards are redrawn
    entirely on every change, and otherwise a slid-out frame would collapse by itself. We do
    not load the picture of a closed one: ten frames on every redraw are of no use to anyone.
    """
    html = _page()

    handler = _js_block(html, '$("chat").addEventListener("click"')
    assert "togglePeek(shotBtn)" in handler
    assert "showShot(shotBtn" not in handler, "the label opens the frame at once again, bypassing the slide-out"
    assert 'showShot(peekImg.closest(".shotpeek").dataset.shot)' in handler

    assert 'class="shotpeek' in _js_function(html, "peekHTML")
    assert "data-src" in _js_function(html, "peekHTML")
    assert "shotOpen.has(" in _js_function(html, "peekHTML")
    toggle = _js_function(html, "togglePeek")
    assert "shotOpen.add(" in toggle and "shotOpen.delete(" in toggle

    flags = _js_function(html, "flagsHTML")
    assert "aria-expanded" in flags and "shotOpen.has(" in flags


def test_a_frame_in_the_feed_opens_inside_the_app_not_in_a_new_tab():
    """A frame from the conversation feed used to open with `window.open` in a new tab, so the
    person left the app. Now it opens in the same layer over the page as the frame from the
    answer card.
    """
    handler = _js_block(_page(), '$("raw").addEventListener("click"')

    assert 'showShot(img.closest(".row.shot").dataset.shot)' in handler
    assert "window.open" not in handler, "a frame from the feed opens in a new tab again"


def test_a_frame_in_the_feed_opens_only_from_the_picture_not_the_whole_row():
    """A click on the time, on the word "Screen" or on the empty space next to the frame
    preview must not open the frame, only a click on the picture itself.
    """
    handler = _js_block(_page(), '$("raw").addEventListener("click"')

    assert 'closest?.(".row.shot img")' in handler
    assert 'closest?.(".row.shot")' not in handler, "the frame opens with a click on the whole row"


def test_a_frame_row_is_built_like_a_speech_row():
    """A frame row is a feed row just like a speech line: a "?" to the left of the time and no
    caption saying it went to the model (the frame is in the feed precisely because it went).
    The "?" here asks about the screen.

    The "?" is handled before the click on the picture: the order of checks in the feed
    handler must not change, otherwise the branches will start overriding each other.
    """
    html = _page()

    row = _js_function(html, "shotRow")
    assert "went to the model" not in row
    assert 'class="kb"' not in row
    assert row.index("askScreenBtn") < row.index('class="t"'), "the \"?\" must stand to the left of the time"

    handler = _js_block(html, '$("raw").addEventListener("click"')
    assert handler.index('closest?.(".ask-raw")') < handler.index('closest?.(".row.shot img")'), (
        "a click on the \"?\" in a frame row would open the frame instead of asking"
    )
    assert '"/ask/screen"' in handler, "the \"?\" of a frame must ask about the screen"


def test_the_question_icon_sits_a_whole_number_of_pixels_off_the_baseline():
    """The "?" to the left of the time in the frame row stood one pixel lower than the digits,
    while in the speech row it was centered. The cause is `vertical-align: middle`: it puts
    the center of the icon half an x-height above the baseline (3.5px at 14px), which is half
    a pixel. Text is drawn at whole pixels and the icon at a fractional position, and
    depending on the fraction at the top of the row (a preview 146.25px high shifts all the
    rows below it) the icon either landed on the center of the digits or drifted by a pixel.
    A whole-pixel offset gives the same difference in every row.
    """
    rule = re.search(r"\.row-head \.ask-raw \{([^}]*)\}", _page())

    assert rule, "the \"?\" in a feed row lost its alignment rule"
    align = re.search(r"vertical-align:\s*([^;]+);", rule.group(1))
    assert align and re.fullmatch(r"-?\d+px", align.group(1).strip()), (
        f"the \"?\" is aligned by {align and align.group(1)!r}, but a whole number of pixels is needed"
    )


def test_detached_windows_show_the_same_answer_card_as_the_main_window():
    """Detached windows (chat, "all at once") drew the answer as a bubble without a border and
    without "Again"/"Edit"; now they show the same card as the main
    window, with the buttons, the frame label and the slide-out frame. One layout for all
    windows: there must be no branches on `SOLO` in `renderChat`.
    """
    html = _page()

    chat = _js_function(html, "renderChat")
    assert "SOLO" not in chat, "renderChat has its own layout for detached windows again"
    assert 'class="io question' in chat and 'class="io output' in chat
    assert "q-bubble" not in html and "a-text" not in html


def test_the_gap_under_the_last_message_equals_the_gap_above_the_first_in_every_window():
    """The feed has its own padding of 10px at the top and bottom, and the card has another 12px
    at the bottom: under the last message it came out 22px against 10px above the first. The
    last card must have no bottom margin, neither in the main window nor in the detached
    ones.
    """
    html = _page()

    assert re.search(r"(?<![\w\]\)-])\.turn:last-child \{ margin-bottom:0; \}", html)
    assert "body[data-solo] .turn:last-child" not in html, "the rule is only for detached windows again"


def test_question_arrows_are_set_before_the_chat_scrolls_to_the_bottom():
    """The arrow "show the whole question" is taller than a line of text (22px against 21px) and
    is turned on by measuring the width. If it is turned on after scrolling to the bottom,
    the feed does not stay at the very bottom: the open window could be scrolled a couple
    more pixels. So the markup is prepared in `onChange`, before the scroll.
    """
    html = _page()

    set_chat = _js_function(html, "setChat")
    assert set_chat.index("onChange?.()") < set_chat.index("box.scrollTop = box.scrollHeight")

    render = _js_function(html, "renderChat")
    assert "setChat(answersHTML, () => {" in render
    assert "updateQuestionToggles();" in render[render.index("setChat(answersHTML") :]


def test_the_label_icon_and_arrow_sit_a_whole_pixel_lower_than_the_button_center():
    """The picture icon and the arrow in the "with screenshot" label came
    out higher than the words (measured: by 1.7px, since the middle of lowercase letters lies
    below the middle of the line). Both are lowered by one whole pixel with a shared rule.
    The offset is done with `top`, not `transform`, because the arrow is turned with
    `transform`.
    """
    html = _page()

    rule = re.search(r"\.turn button\.withshot > svg \{([^}]*)\}", html)
    assert rule, "the label's icon and arrow lost their downward offset"
    top = re.search(r"top:\s*(\d+)px", rule.group(1))
    assert top and int(top.group(1)) >= 1, "the icon and the arrow are not lowered by a whole number of pixels"
    assert "transform" not in rule.group(1)


def test_a_detached_window_can_be_dragged_by_the_card_action_row():
    """The window is dragged by the panel, except for the `.turn`/`.row` "pockets", where a
    click is left to text selection. The card takes up almost the whole window (22% of free
    space before, 13% with cards), and there was almost nothing to drag it by. The free space
    of the action row and the "thinking" header drag it; buttons, the frame and links do not.
    """
    html = _page()

    handler = _js_block(html, 'for (const id of ["chat", "raw"])')
    assert '".actions, .head"' in handler
    assert '"button, a, img, input"' in handler, "buttons and the frame must not drag the window"
    assert "e.stopPropagation()" in handler

    assert re.search(r"body\[data-solo\] \.panel \.turn > \.actions,\s*body\[data-solo\] \.panel \.turn > \.head \{ cursor:move", html)


def test_a_feed_standing_at_the_bottom_follows_new_frames_while_they_load():
    """A frame without dimensions has zero height until it loads: the feed was scrolled "down"
    at the moment of drawing, and the frame that finished loading afterwards went past the
    edge, leaving hundreds of pixels to the bottom, and the feed did not follow the frame.
    Space is reserved for the frame, and a feed that stood at the bottom is pushed back to
    the bottom when each picture loads (`load`, in the capture phase, because it does not
    bubble).
    """
    html = _page()

    helper = _js_function(html, "keepBottomWhileImagesLoad")
    assert 'addEventListener("load"' in helper and "}, true);" in helper
    assert 'addEventListener("scroll"' in helper
    assert 'keepBottomWhileImagesLoad($("raw"))' in html
    assert 'keepBottomWhileImagesLoad($("chat"))' in html
    assert re.search(r"\.row\.shot img \{[^}]*aspect-ratio:\s*auto 16 / 9", html)


def test_a_photo_from_the_clipboard_is_pasted_compressed_and_sent_only_with_a_typed_question():
    """Ctrl+V with a picture puts a thumbnail to the left of the field; it goes out only with
    "Ask" and Enter. "Again" and "?" on speech lines do not take the
    photo, otherwise a photo pasted just now would go out with someone else's old question.
    Before sending, the picture is recompressed into JPEG: the server accepts only that, and
    a full-screen PNG screenshot is several megabytes.
    """
    html = _page()

    paste = html[html.index('document.addEventListener("paste"') :][:1800]
    assert "createImageBitmap" in html and '"image/jpeg"' in html
    assert 'i.type.startsWith("image/")' in paste
    assert "e.preventDefault()" in paste
    # Only if the server said it can: an old server would ignore the photo.
    assert "!state?.can_paste_image" in paste

    assert "image_b64" in html
    # Exactly two places take the photo: the button and Enter.
    assert html.count('takeAttached()') >= 3          # definition + two calls
    assert 'ask("/ask", takeAttached())' in html
    assert html.count('ask("/ask", takeAttached())') == 2
    # The other calls of `ask` do not pass it (`null`) and name the question explicitly: they do
    # not occupy the input field.
    assert 'await ask("/ask", null, realCause(a));' in html
    assert 'await ask(screen ? "/ask/screen" : "/ask", null, text);' in html

    assert 'class="attach" id="attach"' in html


def test_a_pasted_photo_is_called_a_photo_not_a_screen_frame():
    html = _page()

    flags = _js_function(html, "flagsHTML")
    assert 'a.image_src === "paste"' in flags and '"with photo"' in flags
    row = _js_function(html, "shotRow")
    assert 'sh.kind === "paste"' in row and '"Photo"' in row


def test_ask_does_not_take_the_click_event_as_its_address():
    """`ask(path)` now takes an address, while the button handler receives the click event. Pass
    `ask` to a button as is, and the question would go to the address "[object MouseEvent]":
    silently, without a single error on the page.
    """
    html = _page()

    assert '$("ask").onclick = () => ask("/ask", takeAttached());' in html
    assert '$("ask").onclick = ask;' not in html


# Answers are a list of `image_kb`, frames are pairs (id, kb); we expect the frame id for
# each answer, or None.
CASES = {
    "everything in place": ([10, 20, 30], [(1, 10), (2, 20), (3, 30)], [1, 2, 3]),
    "old frames evicted": ([10, 20, 30], [(2, 20), (3, 30)], [None, 2, 3]),
    # A request the model did not answer leaves a frame without a card.
    "a frame without a card in the middle": ([10, 30], [(1, 10), (2, 20), (3, 30)], [1, 3]),
    "an answer without a picture": ([10, 0, 30], [(1, 10), (3, 30)], [1, None, 3]),
    # The screen did not change, so all frames have the same size, but the pairs are still
    # different.
    "equal sizes": ([50, 50, 50], [(1, 50), (2, 50), (3, 50)], [1, 2, 3]),
    # The middle one has no frame; it must not "eat" the frames of the older answers.
    "one answer has no frame": ([10, 99, 30], [(1, 10), (3, 30)], [1, None, 3]),
    "no frames at all": ([10, 20], [], [None, None]),
}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page JS")
def test_every_answer_gets_the_frame_it_went_with():
    """The server returns answers and frames as two independent lists with no common key; the
    pairs are assembled in order from the end and by size.
    """
    cases = [
        {
            "answers": [{"image_kb": kb} for kb in answers],
            "shots": [{"id": i, "kb": kb} for i, kb in shots],
        }
        for answers, shots, _ in CASES.values()
    ]
    script = (
        _js_function(_page(), "answerShots")
        + f"\nconst cases = {json.dumps(cases)};\n"
        # Holes in a sparse array -> null, so that the lists can be compared by the length of the
        # answers.
        + "console.log(JSON.stringify(cases.map(c => {"
        + " const ids = answerShots(c.answers, c.shots);"
        + " return c.answers.map((_, i) => ids[i] ?? null); })));"
    )
    done = subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=True
    )

    got = dict(zip(CASES, json.loads(done.stdout), strict=True))
    want = {name: expected for name, (_, _, expected) in CASES.items()}
    assert got == want
