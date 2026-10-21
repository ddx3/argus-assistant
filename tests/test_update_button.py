"""The "Update" button: at the top of the page, on any page of the app.

Requirement: a bright button, somewhere at the top, so that it is always there on any
page.

It is drawn by the script `update-button.js` from the response of `/update/state`
(`update_service.py`). What is checked here: the script is included everywhere, it
parses, it takes from the server only what the server gives, and it shows nothing
technical.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "argus" / "ui" / "static"


def js() -> str:
    return (STATIC / "update-button.js").read_text(encoding="utf-8")


def test_the_script_parses():
    """An extra backtick in a CSS string quietly kills the entire script (this has happened
    before with `winbar.js`).
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    done = subprocess.run([node, "--check", str(STATIC / "update-button.js")], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("page", ["index.html", "home.html", "record.html", "admin.html"])
def test_every_page_of_the_app_carries_the_button(page):
    """The button "on any page": a page without it would leave a person without the update
    exactly where they are stuck (the sign-in screen is a page too)."""
    assert '<script src="/update-button.js" defer></script>' in (STATIC / page).read_text(encoding="utf-8"), page


@pytest.fixture
def http(monkeypatch):
    """A real app server with no windows and no hardware."""
    from fastapi.testclient import TestClient

    from argus.app import Argus
    from argus.core.config import Config
    from argus.ui.server import build

    monkeypatch.setattr("argus.desktop._PANELS", None)
    app = Argus(Config(), with_screen=False, with_brain=False)
    with TestClient(build(app)) as client:
        yield client


def test_the_server_serves_the_script(http):
    answer = http.get("/update-button.js")
    assert answer.status_code == 200 and "javascript" in answer.headers["content-type"]
    assert "/update/state" in answer.text


def test_the_real_server_answers_the_state_and_refuses_a_bodyless_post(http):
    """Development launch: there is no update, and the button is not drawn; the routes are in
    place.
    """
    state = http.get("/update/state").json()
    assert state["available"] is False and state["can_install"] is False
    assert http.post("/update/install").status_code == 422, "without a JSON body a foreign form cannot press anything"
    assert http.post("/update/install", json={}).json()["ok"] is False


def test_it_reads_only_what_the_service_gives(tmp_path):
    """The page must not wait for fields that the service does not return."""
    from argus import update_service

    given = {"available", "phase", "percent", "can_install", "busy"}
    used = set(re.findall(r"\b(?:state|s)\.([a-z_]+)\b", js())) & {"available", "phase", "percent", "can_install", "busy", "version", "reason", "error"}
    assert used <= given, used - given
    code = re.sub(r"(?m)^\s*//.*$", "", re.sub(r"/\*.*?\*/", "", js(), flags=re.S))
    assert "version" not in code.replace("--upd", "")
    assert update_service.UpdateService.snapshot  # the service is in place


def test_it_posts_json_so_a_foreign_page_cannot_press_it():
    src = js()
    assert '"content-type": "application/json"' in src and "body: JSON.stringify(" in src
    assert '"/update/install"' in src and '"/update/open"' in src


def test_the_font_declaration_is_valid_so_the_designed_size_is_not_silently_dropped():
    """`font:600 13px/1 inherit`: the family cannot be the word `inherit` inside the shorthand,
    so the whole rule was dropped and the button was drawn at 14px in normal weight."""
    src = js()
    assert "font:600 13px/1 inherit" not in src
    assert "font-weight:600; font-size:13px; line-height:1; font-family:inherit;" in src


# The real button code in node with a fake page: what it sends to the server on a press
# (the way `test_speech_playback.py` runs the player from the page). `state` is what
# `/update/state` will answer right now, `installAnswer` is the answer to the POST; `posts`
# is everything the page sent other than state polling.
HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const scenario = process.argv[3];

const posts = [];
const labels = [];
let state = { available: true, phase: "idle", percent: null, can_install: true, busy: false };
let installAnswer = { ok: true, phase: "downloading", percent: 0 };
const timers = [];

function el(tag) {
  return {
    tag, dataset: {}, listeners: {}, children: [], parentNode: null, innerHTML: "", disabled: false, textContent: "",
    style: { setProperty() {} }, classList: { add() {}, remove() {} },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    insertBefore(c) { c.parentNode = this; this.children.push(c); return c; },
    remove() { this.parentNode = null; }, toggleAttribute() {}, querySelector() { return null; }, hasAttribute() { return false; },
  };
}
global.window = {};
window.top = window;
global.document = {
  body: el("body"), head: el("head"), hidden: false, createElement: el, querySelector: () => null, addEventListener() {},
};
global.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
global.clearTimeout = () => {};
global.fetch = (path, opts) => {
  let answer;
  if (path === "/update/state") {
    answer = JSON.parse(JSON.stringify(state));
  } else {
    posts.push({ path, body: JSON.parse(opts.body) });
    answer = JSON.parse(JSON.stringify(installAnswer));
  }
  return Promise.resolve({ json: () => Promise.resolve(answer) });
};
vm.runInThisContext(source);

const settle = async () => { for (let i = 0; i < 12; i++) await new Promise((r) => setImmediate(r)); };
const button = () => document.body.children.find((c) => c.tag === "button");
const click = async () => { button().listeners.click(); await settle(); };
const poll = async () => { timers[timers.length - 1].fn(); await settle(); };      // the polling timer fired
const seeLabel = () => labels.push(button().innerHTML.replace(/<svg.*?<\/svg>/, "").replace(/<\/?span>/g, ""));   // label without the icon

const scenarios = {
  async idle() { await click(); },
  async stale_busy() {
    state.busy = true;              // the page does not know about it yet: its last poll was before the recording started
    await click();                  // the first press only asks
    await click();                  // the second one agrees
  },
  async failed_twice() {
    state.phase = "failed";
    await poll();
    seeLabel();                     // a press will retry right here, as the label says
    await click();                  // first: one more attempt
    state.phase = "failed";
    await poll();
    seeLabel();                     // the retry failed too: now a press will open the download page
    await click();                  // second: the download page
  },
  async failed_cannot_install() { state.can_install = false; state.phase = "failed"; await poll(); seeLabel(); await click(); },
  async cannot_install() { state.can_install = false; await poll(); await click(); },
  async server_sees_a_recording() {
    installAnswer = { ok: false, confirm: true, phase: "idle", percent: null, can_install: true, busy: true };
    await click();
  },
};

(async () => {
  timers[0].fn();                   // first poll (1.2 s after the page loads)
  await settle();
  await scenarios[scenario]();
  console.log(JSON.stringify({ posts, labels }));
})();
"""


def _run(tmp_path: Path, scenario: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run(
        [node, str(harness), str(STATIC / "update-button.js"), scenario], capture_output=True, text=True, encoding="utf-8",
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _press(tmp_path: Path, scenario: str) -> list[dict]:
    return _run(tmp_path, scenario)["posts"]


def test_a_press_installs_when_nothing_is_recording(tmp_path):
    assert _press(tmp_path, "idle") == [{"path": "/update/install", "body": {"confirm": False}}]


def test_it_asks_the_server_again_before_deciding_about_a_recording(tmp_path):
    """The "recording is running" flag can be more than a minute old (polling every 60 s):
    recording was just turned on while the page did not know, and a press updated right in the
    middle of it. Now the first press asks for the state anew and only asks."""
    assert _press(tmp_path, "stale_busy") == [{"path": "/update/install", "body": {"confirm": True}}], \
        "the first press sends nothing, the second one comes with a confirmation"


def test_when_the_server_sees_a_recording_the_page_asks_instead_of_installing(tmp_path):
    assert _press(tmp_path, "server_sees_a_recording") == [{"path": "/update/install", "body": {"confirm": False}}]


def test_after_a_failure_the_first_press_retries_and_the_second_opens_the_download_page(tmp_path):
    """A single failure (the network blinked, the hash did not match) left "Failed — download"
    in place until restart: a retry was unreachable.
    """
    assert _press(tmp_path, "failed_twice") == [
        {"path": "/update/install", "body": {"confirm": False}},
        {"path": "/update/open", "body": {}},
    ]


def test_the_label_after_a_failure_says_what_the_press_does(tmp_path):
    """The caption "Failed — download" stayed even after the first failure,
    although the first press retries right there.
    """
    assert _run(tmp_path, "failed_twice")["labels"] == ["Failed — try again", "Failed — download"]
    run = _run(tmp_path, "failed_cannot_install")
    assert run["labels"] == ["Failed — download"], "it cannot be installed from here - the press opens the download page"
    assert run["posts"] == [{"path": "/update/open", "body": {}}]


def test_where_installing_is_impossible_the_press_opens_the_download_page(tmp_path):
    assert _press(tmp_path, "cannot_install") == [{"path": "/update/open", "body": {}}]


def test_it_asks_before_interrupting_a_recording_and_never_shows_technical_text():
    src = js()
    assert "state.busy" in src and "Recording in progress. Update?" in src
    for word in ("exception", "traceback", "http://", "sha256", "signature"):
        assert word not in src.lower(), word


def test_it_stays_out_of_detached_windows_and_frames():
    src = js()
    assert "window.top !== window" in src
    assert 'hasAttribute("data-solo")' in src


def test_it_sits_in_the_window_strip_or_floats_when_there_is_none():
    src = js()
    assert 'document.querySelector(".winbar")' in src and ".winbar-btns" in src
    assert ".upd.floating { position:fixed;" in src


def test_its_colors_cannot_be_overridden_by_the_generic_button_style():
    """The generic button rule painted it gray under the cursor and dimmed it when disabled; it
    must stay bright.
    """
    src = js()
    assert "background:var(--upd-bg) !important" in src
    assert ".upd:disabled { cursor:default; opacity:1 !important; }" in src


def test_every_visible_string_has_a_translation_in_all_languages():
    strings = ["Update", "Download", "Downloading…", "Restarting…", "Failed — download", "Failed — try again",
               "Recording in progress. Update?"]
    for text in strings:
        assert text in js(), text
    for lang in ("ru", "uk", "es", "pt", "de", "fr", "tr", "pl"):
        words = json.loads((STATIC / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for text in strings:
            assert words.get(text), (lang, text)
