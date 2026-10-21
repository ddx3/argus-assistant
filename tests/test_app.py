"""Checks of the pipeline assembly and of the data for the dashboard.

The hardware is not touched here: we check that the assembly does not drift apart and that
the state snapshot contains everything the dashboard exists for. It easily breaks silently:
a field gets renamed and a number simply disappears from the interface.
"""

from __future__ import annotations

import pytest

from argus.app import AppState, Argus, Stage
from argus.brain.base import Answer
from argus.core.config import Config
from argus.core.events import ScreenBlock, ScreenEvent, Source, SpeechEvent
from argus.ui.server import snapshot

NS = 1_000_000_000


# --------------------------------------------------------------------------
# Stage timing
# --------------------------------------------------------------------------


def test_stage_tracks_last_and_average():
    """The overall latency says nothing — a breakdown by stage is needed."""
    st = Stage("model")
    st.add(100)
    st.add(300)
    assert st.last_ms == 300
    assert st.avg_ms == 200
    assert st.count == 2


def test_stage_without_work_has_no_average():
    assert Stage("empty").avg_ms == 0.0


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def test_parts_can_be_switched_off():
    """The screen and the model must be switchable off: the pipeline must still assemble
    without them.
    """
    cfg = Config()
    app = Argus(cfg, with_screen=False, with_brain=False)
    assert app.screen is None
    assert app.vision is None
    assert app.assistant is None
    assert app.audio is not None
    assert app.speech is not None


def test_screen_is_captured_even_when_it_is_not_read():
    """The model's screen snapshot is always needed; constant reading is not needed everywhere.

    Reading is switched off, not capture: otherwise the picture, which is the reason to look at
    the screen at all, would disappear together with the unneeded recognition.
    """
    cfg = Config()
    cfg.ocr.enabled = False

    app = Argus(cfg, with_brain=False)
    assert app.screen is not None
    assert app.vision is None


def test_full_assembly_has_everything():
    app = Argus(Config())
    assert app.screen is not None
    assert app.vision is not None
    assert app.assistant is not None
    # The timeline of the brain and of the app must be one and the same, otherwise the dashboard
    # would show one thing and the prompt would get another.
    assert app.assistant.timeline is app.timeline


async def test_ask_without_brain_is_refused_not_crashed():
    app = Argus(Config(), with_screen=False, with_brain=False)
    answer = await app.ask("question")
    assert not answer.ok
    assert "turned off" in answer.error


# --------------------------------------------------------------------------
# State snapshot for the dashboard
# --------------------------------------------------------------------------


@pytest.fixture
def app_with_data() -> Argus:
    app = Argus(Config(), with_screen=False, with_brain=True)
    app.timeline.add_speech(
        SpeechEvent(t=NS, t_end=3 * NS, source=Source.THEM,
                    text="What is the complexity of quicksort?", confidence=0.9)
    )
    app.timeline.add_screen(
        ScreenEvent(t=2 * NS, blocks=[ScreenBlock(text="Question 3 of 10", box=(0, 0, 100, 20))])
    )
    app.state.speech.append(
        SpeechEvent(t=NS, t_end=3 * NS, source=Source.THEM, text="question", confidence=0.9)
    )
    app.state.answers.append(Answer(text="O(n log n)", latency_ms=1800, provider="claude-code"))
    app.state.screen_lines = ["Question 3 of 10"]
    app.state.stages["brain"].add(1800)
    return app


def test_snapshot_has_everything_the_dashboard_shows(app_with_data: Argus):
    s = snapshot(app_with_data)
    for key in (
        "levels", "speech", "answers", "screen", "stages",
        "gate", "timeline", "devices", "problems", "healthy",
    ):
        assert key in s, f"the snapshot has no '{key}': a whole block would vanish from the dashboard"
    # We do not tell the page which model is answering: neither in the snapshot nor in the answer
    # cards.
    assert "model" not in s
    assert all("model" not in a and "provider" not in a for a in s["answers"])


def test_snapshot_is_json_serialisable(app_with_data: Argus):
    """The snapshot goes to the WebSocket: a field that cannot be serialized would bring down
    the whole tab.
    """
    import json

    text = json.dumps(snapshot(app_with_data), ensure_ascii=False)
    assert "O(n log n)" in text
    assert "Question 3 of 10" in text


def test_snapshot_counts_tokens_of_the_real_prompt(app_with_data: Argus):
    """Context usage must be visible before it hits the ceiling."""
    s = snapshot(app_with_data)
    assert s["timeline"]["entries"] == 2
    assert s["timeline"]["tokens"] > 0


def test_snapshot_explains_model_silence(app_with_data: Argus):
    """The filter counters are the only way to understand why there is no answer."""
    gate = app_with_data.assistant.gate
    gate.blocked_not_question = 5
    gate.blocked_cooldown = 2
    s = snapshot(app_with_data)
    assert s["gate"]["not a question"] == 5
    assert s["gate"]["pause"] == 2
    assert s["gate"]["filtered out"] == 7


def test_snapshot_survives_an_empty_pipeline():
    """Right after launch there is no data yet — the dashboard must not crash."""
    app = Argus(Config(), with_screen=False, with_brain=False)
    s = snapshot(app)
    assert s["speech"] == []
    assert s["answers"] == []
    assert s["gate"] == {}
    assert s["frame_gate"] == {}


def test_problems_are_reported_before_start():
    """Until the threads are up, the pipeline is honestly considered not alive."""
    app = Argus(Config(), with_screen=False, with_brain=False)
    assert not app.healthy
    assert app.problems()


def test_state_keeps_history_bounded():
    """Utterances and answers pile up for hours — memory must not grow."""
    st = AppState()
    for i in range(200):
        st.speech.append(
            SpeechEvent(t=i * NS, t_end=(i + 1) * NS, source=Source.ME, text=f"line {i}")
        )
        st.answers.append(Answer(text=f"answer {i}"))
    assert len(st.speech) == 50
    assert len(st.answers) == 20
    assert st.speech[-1].text == "line 199"


def test_dashboard_page_is_shipped():
    """The page lives in the package: without it the dashboard returns nothing."""
    from argus.ui.server import STATIC

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "/ws" in html
    # The preview is a continuous stream, not polling on a timer: polling looked like a slideshow,
    # and polling more often would only breed requests.
    assert "screen.mjpg" in html
    # Panels without captions: "Answers", "Screen" and "Conversation"
    # are not written anywhere (the content speaks for itself), so we check by id, not by text.
    for panel_id in ('id="p-chat"', 'id="p-screen"'):
        assert panel_id in html


def test_dashboard_has_the_control_panel():
    """The switch and the per-source checkboxes are what gets controlled by hand."""
    from argus.ui.server import STATIC

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "/control" in html
    for label in ("Microphone", "System audio", "Screen"):
        assert label in html
    # The question is visible on its own line above the answer: it collapses to one line with an
    # ellipsis if it is long, and expands on a click on the arrow. The raw prompt that went to the
    # model is no longer shown on the card: for a person it was an ordinary technical wall of text,
    # not information.
    assert '<div class="io question' in html
    # The answer tag has no "think longer" mark: the light bulb has been removed (requirement:
    # remove it altogether).
    assert '<div class="tag">answer</div>' in html
    assert "BULB" not in html
    assert 'class="withdeep"' not in html
    assert "a.cause" in html
    # The same label as for the answer, only in a muted color: before, an answer had a caption
    # line of its own while the question had only text with an arrow, and side by side that read
    # as two different styles of one card.
    assert '<div class="tag">question</div>' in html
    # The arrow has nothing to expand if the whole question already fits on one line. This is
    # measured in the collapsed state (`scrollWidth` against `clientWidth`), without relying on
    # the state in which the card happens to be drawn now: `.expanded` would otherwise mask the
    # truncation.
    assert "toggle.hidden = p.scrollWidth <= p.clientWidth" in html
    # One expansion mechanism for all windows, keyed by `data-key`, not a copy of its own for each
    # kind of card.
    assert '.q-toggle[hidden] { display:none; }' in html


async def test_web_app_exposes_its_routes():
    app = Argus(Config(), with_screen=False, with_brain=False)
    from argus.ui.server import build

    web = build(app)
    paths = {route.path for route in web.routes}
    assert {"/", "/ask", "/state", "/ws", "/screen.jpg", "/screen.mjpg"} <= paths




def test_every_control_has_a_click_handler():
    """The checkbox handler was once lost while editing neighboring code.

    The class on the checkbox kept changing on redraw, so from the outside it looked like "the
    checkboxes don't get checked", though the press simply went nowhere. No error, no trace in
    the console.

    Now there is one handler for the whole row, through delegation: losing it entirely is
    harder, and the test catches it if it is lost anyway.
    """
    from argus.ui.server import STATIC

    js = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '$("sources").onclick' in js, "the row of checkmarks has no click handler"
    assert "[el.dataset.k]: !state.controls.sources[el.dataset.k]" in js
    assert '$("power").onclick' in js, "the power switch has no click handler"
    # There is no separate window-choice button any more: the choice opens when the "Screen"
    # checkbox itself is pressed, past the square. We check that this branch is in place:
    # if it is lost, we get a checkbox that toggles and a window choice that opens from nothing.
    # The window choice opens by pressing the source itself, past the square, both for the screen
    # and for the sound. If this branch is lost, we get checkboxes that toggle and a choice that
    # opens from nothing.
    assert "openPicker(" in js, "pressing a source no longer opens the window choice"
    assert 'el.dataset.k === "screen" || el.dataset.k === "loopback"' in js, (
        "the window choice opens on the wrong sources"
    )


def test_the_dashboard_has_a_microphone_device_picker():
    """The same pair of zones as for the screen and the sound: the square toggles, the name opens
    the choice. The name is the static "Microphone", like "Screen" and "System
    audio" next to it; the list is a real `<select>`, by the same path as in
    /admin (`/audio/devices`, `mic_device` in `/control`): this duplicates logic, not code,
    since the server already knew how to do this for the settings.

    The open popup of the select is painted by `chrome.js` ("our own list instead of the native
    popup", now shared by both pages: it moved there from admin.js when the list was also
    needed on the dashboard): the native one in WebView2 takes no styling at all. The select
    itself is not visible: it lies over the name and the arrow as a transparent layer.
    """
    from argus.ui.server import STATIC

    js = (STATIC / "index.html").read_text(encoding="utf-8")
    # The button name is static, like its neighbors'. This has been confused once: the chosen
    # device was put straight into the name, and "Microphone" disappeared from the
    # screen.
    assert '<span class="name">Microphone</span>' in js
    assert 'id="mic-dev"' in js
    # It stands in the sources row itself, next to the "Microphone" checkbox and before
    # the "System audio" checkbox, not off to the side by the level bar (it was
    # there earlier and did not stick: an empty row instead of a checkbox read as "the choice has
    # disappeared").
    mic_at = js.index('data-k="mic"')
    dev_at = js.index('id="mic-dev"')
    loopback_at = js.index('data-k="loopback"')
    assert mic_at < dev_at < loopback_at
    # A click on the select layer is its own thing, not a source toggle: otherwise every opening
    # of the list would also switch the microphone off or on.
    assert 'if (e.target.closest("select")) return;' in js
    assert "/audio/devices" in js
    # It is read anew on every opening, not only once: a headset that appeared or went to sleep
    # between visits would otherwise not be visible.
    assert 'micDev.addEventListener("mousedown", loadMics)' in js
    # And immediately on page load, without waiting for the first click.
    assert "loadMics();\nmicDev.addEventListener" in js
    assert 'micDev.addEventListener("change", () => control({ mic_device: micDev.value }))' in js


def test_the_combo_popup_lives_in_the_shared_chrome_not_duplicated():
    """The open list of a select is shared infrastructure (chrome.js/chrome.css, both pages), not
    a copy of its own on the dashboard and not one left over in /admin: two instances of the
    same `mousedown` handler on `document` would chase the same select and drown each other
    out.
    """
    from argus.ui.server import STATIC

    chrome_js = (STATIC / "chrome.js").read_text(encoding="utf-8")
    assert "function openCombo" in chrome_js
    assert "function closeCombo" in chrome_js
    # The list is wider than the select when explicitly asked (`data-combo-min-width`): on the
    # dashboard the microphone select is sized for the short word "Microphone", while
    # device names can be noticeably longer.
    assert "comboMinWidth" in chrome_js

    admin_js = (STATIC / "admin.js").read_text(encoding="utf-8")
    assert "function openCombo" not in admin_js, "a list of its own is still left in admin.js"
    assert "function closeCombo" not in admin_js

    index_js = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "function openCombo" not in index_js, "a list of its own is duplicated on the dashboard"


def test_the_combo_popup_can_anchor_to_a_wider_label():
    """The invisible select on the dashboard is narrower than the whole "Microphone"
    button: it deliberately does not cover the off checkbox (the square), otherwise a click on
    it would open the list instead of toggling. But the popup's coordinates are taken from the
    select itself by default, so the list would come out shifted from the button's edge instead
    of sitting right under it. `data-combo-anchor="label"` asks to take the coordinates from
    the whole button (`<label>`): it is opt-in, not general behavior. Ordinary visible selects
    in /admin do not set it and get exactly what they got before.
    """
    from argus.ui.server import STATIC

    chrome_js = (STATIC / "chrome.js").read_text(encoding="utf-8")
    assert 'select.dataset.comboAnchor === "label"' in chrome_js
    assert 'select.closest("label")' in chrome_js

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-combo-anchor="label"' in html


def test_checkbox_tick_is_readable():
    """The checkmark is drawn dark on a color, not dark on dark.

    Regression: the square had its own `color` set, and `currentColor` took it instead of the
    source color, so the background and the checkmark came out the same dark color. The class
    was there and the state was right, but nothing was visible.
    """
    from argus.ui.server import STATIC

    css = (STATIC / "index.html").read_text(encoding="utf-8")
    box = css[css.index(".src .box {") : css.index(".src.on .box {")]
    assert "color:" not in box, "the color on .box overrides the source color"
    # Not a hard-coded value, but `--bg`: the same dark background, only through the theme
    # variable.
    assert "color:var(--bg)" in css[css.index(".src.on .box::after") :][:120]


def test_snapshot_hints_to_sign_in_before_the_first_click(tmp_path):
    """On a freshly downloaded build, people press "recognize" first thing. If the
    reason for the refusal is visible only in the response to that click, the dashboard looks
    simply working until then. `account_hint` must be visible in the snapshot before any click.
    """
    cfg = Config()
    cfg.root = tmp_path
    cfg.account.server = "http://example.invalid"

    app = Argus(cfg, with_screen=False, with_brain=False)
    s = snapshot(app)

    assert s["account_hint"] == "you need to sign in — it's in settings"


def test_snapshot_has_no_hint_without_an_account_server():
    app = Argus(Config(), with_screen=False, with_brain=False)
    assert snapshot(app)["account_hint"] == ""


def test_no_leftovers_of_the_active_window_mode():
    """Tracking of the active window has been removed, so no traces of it should remain.

    A user once saw buttons that were no longer in the code: the browser was holding an old
    page. The test catches real leftovers so that such cases can be put down to the cache right
    away.
    """
    from argus.ui.server import STATIC

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'class="mode' not in html
    assert "Active window" not in html
    assert "capture_mode" not in html
