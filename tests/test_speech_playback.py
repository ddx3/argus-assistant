"""Voiceover of an answer sounds once, and when the answer arrives.

Complaint (2026-10-06): "the text gets read aloud by itself; it should be read aloud only
once, when the answer arrives, but now it may play all the answers at once when I press
something or open an additional window". The server had nothing to do with it: it voices
an answer once, while that answer is being published on the bus (`tests/test_tts.py`).
The page was to blame: the clip queue lived separately in every open page and queued
everything it saw in the state snapshot, and the snapshot holds ALL the answers with their
`audio_ids`. And in the app window autoplay stayed blocked until the document finished
loading (pywebview gives the window a gesture by injecting a script only after that, and
the endless stream of screen frames without a first frame would not let the load finish):
the clips piled up and came out all at once on the first click.

The page is one HTML file with JS inside; the player (`makeSpeechPlayer`) is made as a
factory with injected dependencies, and these tests run the same function that sits in
the page through `node`, the way `test_shot_view.py` runs the frame pairing. A real
browser (locks between windows, autoplay, hidden windows) is not needed here: it was
measured separately, and here we substitute fake `Audio`, locks and timers that behave
the way the real window drove them: a broken clip gets an `error` event first, then a
`play()` refusal; the lock is one per profile, is handed out in turn, and passes to the
next window by itself when a window is closed.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from argus.ui.server import STATIC

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs node to run the page JS"
)


def _page() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _js_function(html: str, name: str) -> str:
    """Source of a function: from `function name(` to the matching brace of the first `{` after
    it.

    Braces are counted in the text, so inside the player there are no lone curly braces in
    comments and strings, and it has a single parameter (`env`) rather than destructuring by
    fields: the first `{` after the name must open the body.
    """
    start = html.index(f"function {name}(")
    depth = 0
    for i in range(html.index("{", start), len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
    raise AssertionError(f"function {name} has no closing brace")


# Fake environment. `world()` is one "profile": a shared lock and shared timers;
# `w.window_(name, {mode, locks, visible})` is a window with its own player and its own
# `Audio`.
# The `Audio` mode: ok | blocked (NotAllowedError) | hang (play() does not answer) |
# 404 (error, then NotSupportedError) | 404blocked (error, then NotAllowedError);
# a function of the clip id works too.
FAKES = r"""
const tick = () => new Promise((r) => setImmediate(r));
const drain = async () => { for (let i = 0; i < 8; i++) await tick(); };
const A = (...ids) => [{ audio_ids: ids }];

function world() {
  const log = [];
  const timers = [];
  let now = 0;
  let holder = null;
  const waiting = [];
  function next() {
    holder = null;
    while (waiting.length) {
      const w = waiting.shift();
      if (w.owner.dead) continue;
      w.grant();
      return;
    }
  }
  function locksFacade() {
    const owner = { dead: false };
    return {
      request(name, cb) {
        return new Promise((resolve, reject) => {
          if (owner.dead) return;
          const grant = () => {
            const me = { owner };
            holder = me;
            let result;
            try { result = Promise.resolve(cb()); } catch (e) { result = Promise.reject(e); }
            result.then(
              (v) => { if (holder === me) next(); resolve(v); },
              (e) => { if (holder === me) next(); reject(e); },
            );
          };
          if (!holder) grant(); else waiting.push({ owner, grant });
        });
      },
      die() { owner.dead = true; if (holder && holder.owner === owner) next(); },
    };
  }
  function audioFactory(name, mode) {
    const els = [];
    return {
      els,
      make(url) {
        const id = url.replace("/speech/", "").replace(".mp3", "");
        const m = typeof mode === "function" ? mode(id) : mode;
        const el = {
          id, url, killed: false, paused: false, loaded: false, ended: false, onended: null, onerror: null, onpause: null, _pending: null,
          play() {
            log.push(`${name}:${id}`);
            if (m === "ok") return Promise.resolve();
            if (m === "blocked") return Promise.reject({ name: "NotAllowedError" });
            if (m === "hang") return new Promise((res, rej) => { this._pending = { res, rej }; });
            if (m === "404" || m === "404blocked") {
              setImmediate(() => { if (this.onerror) this.onerror(); });
              const why = m === "404" ? "NotSupportedError" : "NotAllowedError";
              return new Promise((res, rej) => setImmediate(() => setImmediate(() => rej({ name: why }))));
            }
            throw new Error("bad mode " + m);
          },
          pause() { this.paused = true; if (this._pending) { this._pending.rej({ name: "AbortError" }); this._pending = null; } },
          removeAttribute(a) { if (a === "src") this.killed = true; },
          load() { this.loaded = true; },
          // like a browser: before `ended` the element itself sends `pause`, already with
          // ended === true
          end() { this.ended = true; if (this.onpause) this.onpause(); if (this.onended) this.onended(); },
          pauseFromOutside() { if (this.onpause) this.onpause(); },
          startLate() { if (this._pending && !this.killed) { log.push(`${name}:LATE:${id}`); this._pending.res(); this._pending = null; } },
        };
        els.push(el);
        return el;
      },
    };
  }
  function window_(name, { mode = "ok", locks = locksFacade(), visible = () => true, activated } = {}) {
    const fac = audioFactory(name, mode);
    const player = makeSpeechPlayer({
      audio: fac.make, locks,
      setTimer: (fn, ms) => { timers.push({ fn, at: now + ms, live: true }); return timers.length - 1; },
      clearTimer: (i) => { if (i !== null && timers[i]) timers[i].live = false; },
      visible, activated, startMs: 1000, endMs: 5000,
    });
    return { name, player, els: fac.els, locks };
  }
  return {
    log, window_, locksFacade,
    advance(ms) { now += ms; timers.filter((t) => t.live && t.at <= now).forEach((t) => { t.live = false; t.fn(); }); },
  };
}
"""


def _run(body: str):
    """Run `body` (the body of an async JS function) and return what it returned."""
    script = (
        _js_function(_page(), "makeSpeechPlayer")
        + FAKES
        + "\n(async () => {\n"
        + body
        + "\n})().then((r) => console.log(JSON.stringify(r)));\n"
    )
    done = subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# --------------------------------------------------------------------------
# Rule 1: sound is news, not an archive
# --------------------------------------------------------------------------


@needs_node
def test_the_first_snapshot_is_history_and_a_rerender_plays_nothing():
    """A new window, F5, a move from the home page into the same session, a language change (it
    reloads all windows): the page started with empty memory, the very first snapshot carried
    all the answers with `audio_ids`, and the clips were still on the server, so the whole
    pile was read aloud from the start. Now the first snapshot after connecting is only
    marked as seen, and what appears later is played.

    A second call with the same snapshot is a `render()` triggered by a button on an old
    snapshot: it must not play anything again.
    """
    got = _run("""
      const w = world(); const a = w.window_("a"); await drain();
      a.player.feed(A("1-0", "1-1")); await drain();
      const first = [...w.log];
      a.player.feed(A("1-0", "1-1", "2-0")); a.player.feed(A("1-0", "1-1", "2-0")); await drain();
      const fresh = [...w.log];
      a.els[0].end(); await drain();
      a.player.feed(A("1-0", "1-1", "2-0")); await drain();
      return { first, fresh, last: w.log };
    """)
    assert got["first"] == []
    assert got["fresh"] == ["a:2-0"]
    assert got["last"] == ["a:2-0"]


@needs_node
def test_a_reconnect_makes_the_next_snapshot_history_again():
    """After a socket reconnect (sleep, server restart) the snapshot again carries everything at
    once: what was made while the socket was closed "already happened" and is not played.
    """
    got = _run("""
      const w = world(); const a = w.window_("a"); await drain();
      a.player.feed(A());
      a.player.feed(A("1-0")); await drain(); a.els[0].end(); await drain();
      a.player.open();
      a.player.feed(A("1-0", "2-0", "3-0")); await drain();
      const after = [...w.log];
      a.player.feed(A("1-0", "2-0", "3-0", "4-0")); await drain();
      return { after, last: w.log };
    """)
    assert got["after"] == ["a:1-0"]
    assert got["last"] == ["a:1-0", "a:4-0"]


@needs_node
def test_clips_play_one_at_a_time_and_in_order_across_answers():
    """There is one queue for all answers: a separate one per answer made the voices overlap."""
    got = _run("""
      const w = world(); const a = w.window_("a"); await drain();
      a.player.feed(A());
      a.player.feed(A("1-0", "1-1")); await drain();
      a.player.feed(A("1-0", "1-1", "2-0")); await drain();
      const one = [...w.log];
      a.els[0].end(); await drain();
      const two = [...w.log];
      a.els[1].end(); await drain();
      return { one, two, three: w.log };
    """)
    assert got["one"] == ["a:1-0"]
    assert got["two"] == ["a:1-0", "a:1-1"]
    assert got["three"] == ["a:1-0", "a:1-1", "a:2-0"]


# --------------------------------------------------------------------------
# Rule 3: one window speaks
# --------------------------------------------------------------------------


@needs_node
def test_only_one_window_speaks_and_a_handoff_replays_nothing():
    """All windows receive the snapshot, the main one and the detached ones (even the "screen"
    one, where answers are not visible), and each played its own: two voices at once. Now the
    voice belongs to the window that took the lock; when it closes, the lock goes to another,
    and that one does not repeat what was already said, but plays only the new.
    """
    got = _run("""
      const w = world(); const a = w.window_("A"); const b = w.window_("B"); await drain();
      a.player.feed(A()); b.player.feed(A());
      a.player.feed(A("1-0")); b.player.feed(A("1-0")); await drain();
      const both = [...w.log];
      a.locks.die(); await drain();
      b.player.feed(A("1-0")); await drain();
      const handoff = [...w.log];
      b.player.feed(A("1-0", "2-0")); await drain();
      return { both, handoff, last: w.log };
    """)
    assert got["both"] == ["A:1-0"]
    assert got["handoff"] == ["A:1-0"]
    assert got["last"] == ["A:1-0", "B:2-0"]


@needs_node
def test_leaving_the_page_frees_the_voice_and_coming_back_starts_from_history():
    """A window that leaves for another page releases the lock itself (`pagehide`), and one that
    returns from the back/forward cache queues behind it and counts the snapshot as an
    archive.
    """
    got = _run("""
      const w = world(); const a = w.window_("A"); const b = w.window_("B"); await drain();
      a.player.feed(A()); b.player.feed(A());
      a.player.leave(); await drain();
      b.player.feed(A("1-0")); await drain();
      const handed = [...w.log];
      a.player.back(true); await drain();
      a.player.feed(A("1-0", "2-0")); b.player.feed(A("1-0", "2-0")); await drain();
      const during = [...w.log];
      b.els[0].end(); await drain();
      return { handed, during, last: w.log };
    """)
    assert got["handed"] == ["B:1-0"]
    assert got["during"] == ["B:1-0"]
    assert got["last"] == ["B:1-0", "B:2-0"]


@needs_node
def test_a_hidden_window_does_not_ask_for_the_voice():
    """A window created hidden does not load media at all, and when it is shown, everything
    deferred starts at once. It does not ask for the voice until it is shown.
    """
    got = _run("""
      const w = world(); let seen = false;
      const a = w.window_("A", { visible: () => seen }); const b = w.window_("B"); await drain();
      a.player.feed(A()); b.player.feed(A());
      a.player.feed(A("1-0")); b.player.feed(A("1-0")); await drain();
      const hidden = [...w.log];
      seen = true; a.player.shown(); await drain();
      b.els[0].end(); await drain();
      a.player.feed(A("1-0", "2-0")); b.player.feed(A("1-0", "2-0")); await drain();
      return { hidden, last: w.log };
    """)
    assert got["hidden"] == ["B:1-0"]
    assert got["last"] == ["B:1-0", "B:2-0"]


@needs_node
def test_without_working_locks_every_window_speaks_as_before():
    """There are no locks (`navigator.locks` does not exist in an insecure context: http that is
    not localhost), or the request was refused or threw: every window has the voice, as
    before the locks; it cannot get worse than that.
    """
    got = _run("""
      const w = world();
      const a = w.window_("a", { locks: undefined }); await drain();
      a.player.feed(A()); a.player.feed(A("1-0")); await drain();
      const bad = { request() { return Promise.reject(Object.assign(new Error("x"), { name: "SecurityError" })); } };
      const b = w.window_("b", { locks: bad }); await drain();
      b.player.feed(A()); b.player.feed(A("2-0")); await drain();
      const throwing = { request() { throw new TypeError("x"); } };
      const c = w.window_("c", { locks: throwing }); await drain();
      c.player.feed(A()); c.player.feed(A("3-0")); await drain();
      return w.log;
    """)
    assert got == ["a:1-0", "b:2-0", "c:3-0"]


# --------------------------------------------------------------------------
# Rule 4: if it failed, we do not accumulate
# --------------------------------------------------------------------------


@needs_node
def test_a_refused_autoplay_is_dropped_not_kept_for_the_next_click():
    """The browser does not allow playing without a gesture: the clip stayed at the head of the
    queue and was retried on every redraw, and the first press released everything
    accumulated at once. Now a refusal means the clip is dropped together with the queue, a
    gesture releases nothing, and fresh audio plays.
    """
    got = _run("""
      const w = world(); const a = w.window_("a", { mode: "blocked" }); await drain();
      a.player.feed(A());
      a.player.feed(A("1-0", "1-1", "2-0")); await drain();
      const first = [...w.log];
      a.player.feed(A("1-0", "1-1", "2-0", "3-0")); await drain();
      const waiting = [...w.log];
      a.player.gesture(); await drain();
      const clicked = [...w.log];
      a.player.feed(A("1-0", "1-1", "2-0", "3-0", "4-0")); await drain();
      return { first, waiting, clicked, last: w.log };
    """)
    assert got["first"] == ["a:1-0"], "one attempt, not one per clip"
    assert got["waiting"] == ["a:1-0"], "while waiting for a gesture, we do not try"
    assert got["clicked"] == ["a:1-0"], "the gesture does not release what has piled up"
    assert got["last"] == ["a:1-0", "a:4-0"], "the fresh one is tried after the gesture"


@needs_node
def test_a_refused_window_hands_the_voice_over_and_replays_nothing():
    """A window without a gesture (autoplay forbidden) yields the voice to a window that is
    allowed, and when it gets a gesture it queues behind the lock and does not play what was
    dropped.
    """
    got = _run("""
      const w = world();
      const a = w.window_("A", { mode: "blocked" }); const b = w.window_("B", { mode: "ok" }); await drain();
      a.player.feed(A()); b.player.feed(A());
      a.player.feed(A("1-0", "1-1", "1-2")); b.player.feed(A("1-0", "1-1", "1-2")); await drain();
      const first = [...w.log];
      a.player.feed(A("1-0", "1-1", "1-2", "2-0")); b.player.feed(A("1-0", "1-1", "1-2", "2-0")); await drain();
      const handed = [...w.log];
      a.player.gesture(); await drain();
      a.player.feed(A("1-0", "1-1", "1-2", "2-0")); await drain();
      return { first, handed, last: w.log };
    """)
    assert got["first"] == ["A:1-0"]
    assert got["handed"] == ["A:1-0", "B:2-0"]
    assert got["last"] == ["A:1-0", "B:2-0"]


@needs_node
def test_a_clip_that_cannot_start_is_killed_and_does_not_start_later():
    """A window created hidden does not start playing at all, and when it is shown, everything
    deferred starts at once. A clip that did not start playing within `startMs` is killed
    (`pause`, `removeAttribute("src")`, `load`), the queue is dropped, and the lock goes
    around to another window.
    """
    got = _run("""
      const w = world();
      const a = w.window_("A", { mode: "hang" }); const b = w.window_("B", { mode: "ok" }); await drain();
      a.player.feed(A()); b.player.feed(A());
      a.player.feed(A("1-0", "1-1")); b.player.feed(A("1-0", "1-1")); await drain();
      const started = [...w.log];
      w.advance(999); await drain();
      const early = [...w.log];
      w.advance(2); await drain();
      const el = a.els[0];
      const killed = el.paused && el.killed && el.loaded;
      el.startLate();                                   // the window was shown
      const late = [...w.log];
      a.player.feed(A("1-0", "1-1", "2-0")); b.player.feed(A("1-0", "1-1", "2-0")); await drain();
      return { started, early, killed, late, last: w.log };
    """)
    assert got["started"] == ["A:1-0"]
    assert got["early"] == ["A:1-0"]
    assert got["killed"] is True
    assert got["late"] == ["A:1-0"], "the killed clip did not start playing later"
    assert got["last"] == ["A:1-0", "B:2-0"], "1-1 was dropped with the queue, the voice is with the second window"


@needs_node
def test_a_lone_window_whose_clip_hangs_gets_the_voice_back_for_the_next_answer():
    w = _run("""
      const w = world();
      let first = true;
      const a = w.window_("A", { mode: () => { const m = first ? "hang" : "ok"; first = false; return m; } });
      await drain();
      a.player.feed(A());
      a.player.feed(A("1-0")); await drain();
      w.advance(1001); await drain();
      a.player.feed(A("1-0", "2-0")); await drain();
      return w.log;
    """)
    assert w == ["A:1-0", "A:2-0"]


@needs_node
def test_a_clip_that_never_ends_is_cut_and_the_stale_queue_is_dropped():
    """A clip that does not end (hangs) is cut off by the end watchdog, and the queue behind it
    is no longer "now": it is dropped instead of coming out at once. A new answer sounds.
    """
    got = _run("""
      const w = world(); const a = w.window_("a"); await drain();
      a.player.feed(A());
      a.player.feed(A("1-0", "1-1")); await drain();
      const before = [...w.log];
      w.advance(5001); await drain();
      const cut = [...w.log];
      a.player.feed(A("1-0", "1-1", "2-0")); await drain();
      return { before, cut, last: w.log, killed: a.els[0].killed };
    """)
    assert got["before"] == ["a:1-0"]
    assert got["cut"] == ["a:1-0"], "the queue behind the cut clip was dropped"
    assert got["last"] == ["a:1-0", "a:2-0"]
    assert got["killed"] is True


@needs_node
def test_a_clip_paused_from_outside_drops_the_queue_instead_of_freezing_the_voice():
    """A clip was stopped from outside (a media key, the system overlay): `ended` will not come,
    and the queue behind it would stand until the end watchdog (90 s) and then come out at
    once.
    """
    got = _run("""
      const w = world(); const a = w.window_("a"); await drain();
      a.player.feed(A());
      a.player.feed(A("1-0", "1-1", "1-2")); await drain();
      a.els[0].pauseFromOutside(); await drain();
      const after = [...w.log];
      a.player.feed(A("1-0", "1-1", "1-2", "2-0")); await drain();
      return { after, last: w.log };
    """)
    assert got["after"] == ["a:1-0"]
    assert got["last"] == ["a:1-0", "a:2-0"]


@needs_node
def test_the_pause_event_before_the_end_is_not_a_stop_from_outside():
    """Before `ended` the element itself sends `pause`, already with `ended === true`. This is
    not a stop from outside: the queue behind the clip is not dropped.
    """
    got = _run("""
      const w = world(); const a = w.window_("a"); await drain();
      a.player.feed(A());
      a.player.feed(A("1-0", "1-1", "1-2")); await drain();
      a.els[0].end(); await drain();
      const second = [...w.log];
      a.els[1].end(); await drain();
      return { second, last: w.log };
    """)
    assert got["second"] == ["a:1-0", "a:1-1"]
    assert got["last"] == ["a:1-0", "a:1-1", "a:1-2"]


@needs_node
def test_a_refusal_before_any_gesture_is_lifted_when_the_gesture_arrives_without_an_event():
    """pywebview gives the window a gesture by injecting a script when the document has finished
    loading; there is no press event then, and `gesture()` is not called. If the first clip
    was refused while there was no gesture, and then one appeared, the window must not stay
    silent until the first click (when working with hotkeys there will never be one): the
    next snapshot lifts the ban.
    """
    got = _run("""
      let active = false;
      const w = world();
      const a = w.window_("A", { mode: () => (active ? "ok" : "blocked"), activated: () => active }); await drain();
      a.player.feed(A());
      a.player.feed(A("1-0", "1-1")); await drain();
      const refused = [...w.log];
      active = true;                                            // the gesture arrived through a script injection, without an event
      a.player.feed(A("1-0", "1-1", "2-0")); await drain();     // the block is lifted; this snapshot could still have been waiting for the lock
      for (const el of a.els) el.end();
      await drain();
      a.player.feed(A("1-0", "1-1", "2-0", "3-0")); await drain();
      return { refused, voiced: w.log.includes("A:3-0") };
    """)
    assert got["refused"] == ["A:1-0"]
    assert got["voiced"] is True


@needs_node
def test_a_refusal_that_came_with_a_gesture_still_waits_for_the_next_real_gesture():
    """A refusal when a gesture is already present is not "no gesture" but a real ban (site
    settings, policy): there is no point trying it on every clip, we wait for a new press.
    """
    got = _run("""
      const w = world();
      const a = w.window_("A", { mode: "blocked", activated: () => true }); await drain();
      a.player.feed(A());
      a.player.feed(A("1-0")); await drain();
      a.player.feed(A("1-0", "2-0")); await drain();
      a.player.feed(A("1-0", "2-0", "3-0")); await drain();
      return w.log;
    """)
    assert got == ["A:1-0"]


@needs_node
def test_a_window_that_left_does_not_ask_for_the_voice_when_it_is_shown_or_clicked():
    got = _run("""
      const w = world(); const a = w.window_("A"); await drain();
      a.player.feed(A());
      a.player.leave(); await drain();
      a.player.shown(); a.player.gesture(); await drain();
      a.player.feed(A("1-0")); await drain();
      return w.log;
    """)
    assert got == []


@needs_node
def test_a_clip_whose_audio_cannot_be_created_is_skipped_and_the_queue_goes_on():
    got = _run("""
      const log = []; const els = [];
      const player = makeSpeechPlayer({
        audio: (url) => {
          if (url.includes("1-1")) throw new Error("no Audio");
          const el = { url, onended: null, onerror: null, play() { log.push(url); return Promise.resolve(); }, pause() {}, removeAttribute() {}, load() {} };
          els.push(el); return el;
        },
        locks: undefined, setTimer: () => null, clearTimer: () => {},
      });
      player.feed(A());
      player.feed(A("1-0", "1-1", "1-2")); await drain();
      els[0].onended(); await drain();
      return log;
    """)
    assert got == ["/speech/1-0.mp3", "/speech/1-2.mp3"]


@needs_node
def test_a_refusal_that_is_not_about_autoplay_skips_the_clip_and_does_not_park_the_window():
    """`NotSupportedError` without an `error` event is not an autoplay ban: the clip is skipped,
    the voice stays with the window, and the queue moves on.
    """
    got = _run("""
      const log = []; const els = [];
      const player = makeSpeechPlayer({
        audio: (url) => {
          const el = { url, onended: null, onerror: null,
            play() { log.push(url); return url.includes("1-0") ? Promise.reject({ name: "NotSupportedError" }) : Promise.resolve(); },
            pause() {}, removeAttribute() {}, load() {} };
          els.push(el); return el;
        },
        locks: undefined, setTimer: () => null, clearTimer: () => {},
      });
      player.feed(A());
      player.feed(A("1-0", "1-1", "1-2")); await drain();
      els[1].onended(); await drain();
      return log;
    """)
    assert got == ["/speech/1-0.mp3", "/speech/1-1.mp3", "/speech/1-2.mp3"]


@needs_node
def test_default_timeouts_kill_a_clip_that_never_starts_and_cut_one_that_never_ends():
    """The other tests set `startMs`/`endMs` themselves; here are the production values (8 and
    90 s).
    """
    got = _run("""
      const timers = []; let now = 0; const els = [];
      const mk = (playResult) => makeSpeechPlayer({
        audio: (url) => { const el = { url, killed: false, onended: null, onerror: null, play: playResult, pause() {}, removeAttribute() { this.killed = true; }, load() {} }; els.push(el); return el; },
        locks: undefined,
        setTimer: (fn, ms) => { timers.push({ fn, at: now + ms, live: true }); return timers.length - 1; },
        clearTimer: (i) => { if (timers[i]) timers[i].live = false; },
      });
      const run = (ms) => { now = ms; timers.filter((t) => t.live && t.at <= now).forEach((t) => { t.live = false; t.fn(); }); };
      const stuck = mk(() => new Promise(() => {}));
      stuck.feed(A()); stuck.feed(A("1-0")); await drain();
      run(3000); await drain();
      const at3 = els[0].killed;
      run(9000); await drain();
      const at9 = els[0].killed;
      const playing = mk(() => Promise.resolve());
      playing.feed(A()); playing.feed(A("2-0", "2-1")); await drain();
      run(9000 + 60000); await drain();
      const cut60 = els[1].killed;
      run(9000 + 95000); await drain();
      return { at3, at9, cut60, cut95: els[1].killed, made: els.length };
    """)
    assert got == {"at3": False, "at9": True, "cut60": False, "cut95": True, "made": 2}


# --------------------------------------------------------------------------
# Rule 5: every clip ends once
# --------------------------------------------------------------------------


@needs_node
def test_a_dead_clip_reports_twice_but_ends_once():
    """A clip that is not on the server (404: pushed out of the last eighty, or voiceover was
    turned off) gives BOTH an `error` event AND a refusal from `play()`. Before, both counted
    it as the end: the second one shifted the queue once more and ate the next clip, and the
    late "not playing" started a second chain, so two phrases played at once.
    """
    got = _run("""
      const w = world(); const a = w.window_("a", { mode: (id) => (id === "1-1" ? "404" : "ok") }); await drain();
      a.player.feed(A());
      a.player.feed(A("1-0", "1-1", "1-2", "1-3")); await drain();
      const first = [...w.log];
      a.els[0].end(); await drain();
      const second = [...w.log];
      const alive = a.els.filter((e) => !e.killed && e.onended).length;
      a.els[2].end(); await drain();
      return { first, second, alive, last: w.log };
    """)
    assert got["first"] == ["a:1-0"]
    assert got["second"] == ["a:1-0", "a:1-1", "a:1-2"]
    assert got["alive"] == 1, "exactly one clip is playing"
    assert got["last"] == ["a:1-0", "a:1-1", "a:1-2", "a:1-3"]


@needs_node
def test_a_late_failure_of_a_finished_clip_does_not_park_the_voice():
    """A broken clip without a gesture: `error`, then `NotAllowedError`. A late refusal of an
    already finished clip must not count as a ban for the following ones.
    """
    got = _run("""
      const w = world(); const a = w.window_("a", { mode: (id) => (id === "1-1" ? "404blocked" : "ok") }); await drain();
      a.player.feed(A());
      a.player.feed(A("1-0", "1-1", "1-2")); await drain();
      a.els[0].end(); await drain();
      const mid = [...w.log];
      a.els[2].end(); await drain();
      a.player.feed(A("1-0", "1-1", "1-2", "2-0")); await drain();
      return { mid, last: w.log };
    """)
    assert got["mid"] == ["a:1-0", "a:1-1", "a:1-2"]
    assert got["last"] == ["a:1-0", "a:1-1", "a:1-2", "a:2-0"]


@needs_node
def test_a_gesture_without_a_refusal_changes_nothing_and_a_missing_audio_does_not_throw():
    got = _run("""
      const w = world(); const a = w.window_("a"); await drain();
      a.player.gesture();
      a.player.feed(A());
      a.player.feed(A("1-0")); await drain();
      const log = [...w.log];
      const broken = makeSpeechPlayer({
        audio: () => { throw new Error("no Audio"); }, locks: undefined,
        setTimer: () => null, clearTimer: () => {},
      });
      broken.feed(A()); broken.feed(A("9-0"));
      return log;
    """)
    assert got == ["a:1-0"]


@needs_node
def test_a_window_shown_later_queues_for_the_voice_and_takes_it_when_the_holder_goes():
    """A window that was shown (`shown`) queues behind the lock and takes the voice when the
    holder releases it; while another holds it, it stays silent.
    """
    got = _run("""
      const w = world(); let seen = false;
      const a = w.window_("A", { visible: () => seen }); const b = w.window_("B"); await drain();
      a.player.feed(A()); b.player.feed(A());
      seen = true; a.player.shown(); await drain();
      a.player.feed(A("1-0")); b.player.feed(A("1-0")); await drain();
      const while_b_holds = [...w.log];
      b.locks.die(); await drain();
      a.player.feed(A("1-0", "2-0")); await drain();
      return { while_b_holds, last: w.log };
    """)
    assert got["while_b_holds"] == ["B:1-0"]
    assert got["last"] == ["B:1-0", "A:2-0"]


@needs_node
def test_clips_made_while_the_page_sat_in_the_cache_are_history_after_it_returns():
    """A page that returns from the back/forward cache sees in the first snapshot everything
    that was made while it slept: this is "already happened", not news.
    """
    got = _run("""
      const w = world(); const a = w.window_("A"); await drain();
      a.player.feed(A());
      a.player.leave(); await drain();
      a.player.back(true); await drain();
      a.player.feed(A("1-0")); await drain();
      const back = [...w.log];
      a.player.feed(A("1-0", "2-0")); await drain();
      return { back, last: w.log };
    """)
    assert got["back"] == []
    assert got["last"] == ["A:2-0"]


@needs_node
def test_a_clip_in_flight_when_the_page_leaves_does_not_block_the_voice_after_it_returns():
    """A clip that was playing at the moment of `pagehide` would not finish by itself from the
    back/forward cache, and the queue behind it would stand until the end watchdog (90 s):
    `leave` cuts it off.
    """
    got = _run("""
      const w = world(); const a = w.window_("A"); await drain();
      a.player.feed(A());
      a.player.feed(A("1-0")); await drain();
      a.player.leave(); await drain();
      a.player.back(true); await drain();
      a.player.feed(A("1-0")); a.player.feed(A("1-0", "2-0")); await drain();
      return { log: w.log, killed: a.els[0].killed };
    """)
    assert got["log"] == ["A:1-0", "A:2-0"]
    assert got["killed"] is True


@needs_node
def test_a_window_that_left_while_waiting_passes_the_lock_on_instead_of_holding_it():
    """A departed window that the lock queue has reached must not hold the lock: the lock moves
    on and the voice goes to a living window, rather than hanging on the departed one.
    """
    got = _run("""
      const w = world();
      const a = w.window_("A"); const b = w.window_("B"); const c = w.window_("C"); await drain();
      a.player.feed(A()); b.player.feed(A()); c.player.feed(A());
      b.player.leave(); await drain();
      a.locks.die(); await drain();
      b.player.feed(A("1-0")); c.player.feed(A("1-0")); await drain();
      return w.log;
    """)
    assert got == ["C:1-0"]


# --------------------------------------------------------------------------
# Wiring in the page (the JS itself cannot be checked without a browser, so we check that
# it is connected)
# --------------------------------------------------------------------------


def test_the_socket_feeds_the_player_before_render_and_nothing_else_plays():
    """The socket feeds the player, not `render()`: that is also called by the control panel's
    buttons on an old snapshot. Connecting arms "the first snapshot is an archive" anew. One
    player plays: there must be no other `new Audio(` for answers in the page, otherwise the
    second path would again bypass all the rules.
    """
    html = _page()

    connect = _js_function(html, "connect")
    assert "ws.onopen = () => speech.open();" in connect
    assert connect.index("speech.feed(") < connect.index("render(s)")

    assert "queueSpeech" not in html and "advanceSpeech" not in html
    assert "speech.feed(" not in _js_function(html, "renderChat"), "the socket feeds the voiceover"
    assert html.count("new Audio(") == 1


def test_the_page_tells_the_player_about_its_own_life():
    """The player needs to know when the window was shown (a hidden one does not ask for the
    lock), when it was pressed (the browser will now let it play), and when the page leaves
    or returns from the back/forward cache (release the lock / the snapshot is an archive
    again).
    """
    html = _page()
    for line in (
        'document.addEventListener("visibilitychange", () => speech.shown());',
        'window.addEventListener("pointerdown", () => speech.gesture(), true);',
        'window.addEventListener("keydown", () => speech.gesture(), true);',
        'window.addEventListener("pagehide", () => speech.leave());',
        'window.addEventListener("pageshow", (e) => speech.back(e.persisted));',
        "activated: () => !!(navigator.userActivation && navigator.userActivation.hasBeenActive),",
    ):
        assert line in html, line
    assert "locks: navigator.locks," in html


def test_the_endless_preview_stream_does_not_start_before_the_document_has_loaded():
    """The frame stream (`<img src="/screen.mjpg">`) is endless, and until it has a first frame
    the document counts as not fully loaded: WebView2 does not send `NavigationCompleted`,
    pywebview does not inject itself into the page, and together with itself the gesture that
    this injection gives, so the first voiceover clips run into the autoplay ban, pile up and
    come out all at once on the first click. So the stream is started after `load`, not right
    during page parsing.
    """
    html = _page()
    assert html.count('.src = "/screen.mjpg"') == 1
    at = html.index('.src = "/screen.mjpg"')
    assert "const startShot = () =>" in html[max(0, at - 80) : at]
    assert 'window.addEventListener("load", startShot, { once: true })' in html
