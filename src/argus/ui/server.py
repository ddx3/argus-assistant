"""Dashboard: M7 acceptance.

This is a gauge, not a product interface. It has one job: to see what is happening
inside the pipeline and where exactly the time goes. The product interface will appear
after a direction is chosen.

Three things it exists for:

* **Latency by stage.** A single total number says nothing. When a reply arrives after
  four seconds, you must see at once that three of them are the model, not recognition;
  otherwise the wrong thing gets fixed.
* **Why the model is silent.** The filter rejects most triggers, and without counters
  its refusals look like a breakage. "Does not look like a question" and "pause after
  the previous request" are different things, and both must be seen.
* **Limit usage.** A subscription counts in five-hour windows, and the number of
  requests per session must be in front of your eyes, not discovered when everything
  is used up.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import errno
import json
import logging
import re
import sys
import time
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse

from argus import design
from argus.account import legal
from argus.assist import settings as assist_settings
from argus.chats import chats_of, manager_of
from argus.chats.routes import attach as attach_chats
from argus.update_service import UpdateService
from argus.update_service import attach as attach_update
from argus.chats.store import valid_id
from argus.live import header as build_header
from argus.live import identity as build_identity
from argus.live import live
from argus.ui import i18n
from argus.ui.guard import LocalGuard
from argus.brain.base import Answer
from argus.context.render import estimate_tokens, render_context
from argus.core import friendly
from argus.core.events import SpeechEvent

if TYPE_CHECKING:
    from argus.app import Argus

log = logging.getLogger(__name__)

# The live folder, if the sources lie next to the build: one version of the page for the
# app and for the browser tab. Details are in `argus/live.py`.
STATIC = live(Path(__file__).parent / "static")


# The version marker lives in `argus/live.py`, where the path itself is computed. Here is
# only the header that is attached to every page served.
NO_CACHE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


def page_headers() -> dict:
    """Page headers: do not cache, and say which version this is.

    The answer to "is what I see current" must come from a single `curl -I` on **any**
    page, not only the dashboard: people look for an edit where it is not visible.
    """
    return {**NO_CACHE, "X-Argus-Build": build_header()}

# The panels that can be detached into their own window. The list is closed: the name
# comes from the browser and goes into the window's address, and opening whatever it names
# is not allowed. "chat" is the model's replies (the name dates from when a timeline merged
# with the conversation briefly lived here; the markup itself went back to the separation
# and did not drag the name along). "live" is not a section of its own in the markup but
# chat+screen at once in a single window; index.html tells the modes apart by this same
# name in `data-solo`.
PANELS = ("chat", "screen", "live")

# What the main window strip (`winbar.js`) may ask of the window.
MAIN_WINDOW_ACTIONS = (
    "state", "minimize", "maximize", "restore", "toggle", "fullscreen", "close", "drag_start", "drag_move",
    # A second launch asks the already running instance to show its window (macOS: another
    # process's window cannot be reached from there, see `mac/desktop.py: focus_existing`).
    # The page does not ask for this.
    "raise",
)

# Limits on the size of a detached window. Smaller, and it cannot be grabbed or closed (it
# has no frame); larger, and it is no longer a panel over the work but a second job.
#
# The lower limit was raised from (200, 120). At that limit the question line broke first:
# three controls in a row (the field, the screen icon, "Ask") with padding and
# a border added up to wider than the 200px they got, and the field shrank to a slit that
# was no longer usable for typing. 360×240 leaves a margin beyond what the question line
# itself and the window control strip need (slider, arrow, pin, cross; that one also needs
# width, even though it does not push the layout but lies on top of it), rather than
# sitting right at their arithmetic minimum.
WINDOW_BOX = (360, 240, 1600, 1200)      # min. width, min. height, max., max.

# The screen panel in "all at once" (`flex:0 0 380px`, index.html) is by itself
# wider than the general limit. Opened over a narrow window, it would squeeze the question
# line and cut the remainder with `overflow:hidden` at `.grid`. That is not one overlapping
# the other, but it is not what was asked for either: part of the input line vanished from
# view. So the "all at once" window with the panel open has its own, higher width floor, not
# the common `WINDOW_BOX`: the panel (380, does not shrink) plus the floor of the question
# line itself next to it. A window with the panel open cannot be squeezed below this sum,
# neither by the corner handle (`clamp_window_size` below) nor by pulling the invisible
# edge (`Panels.set_min_width` in desktop.py, which holds exactly this number, separately
# from the base limit).
#
# The question line here is the same `.ask` as without the panel: the same three controls
# in a row (the field, the screen icon, "Ask"), and whether the screen panel is
# open next to it does not affect them in any way. `.ask` and `#p-chat` share one grid
# column (`grid-template-areas` in index.html), and the panel sits in the adjacent one.
# Before, this had a number of its own, 200, which referred to `.ask{min-width:200px}`,
# and that was not and is not in the markup. And had it been there, it would still have
# been the very limit discussed two paragraphs above: it is not enough for the question
# line, which already broke exactly on it once (`WINDOW_BOX` was raised from 200 to 360 for
# exactly that reason). The floor of the question line is the same in both places, so we
# take it from `WINDOW_BOX[0]`, already proven in the general case, rather than introduce
# a second one that nothing confirms.
LIVE_DRAWER_MIN_WIDTH = 380 + WINDOW_BOX[0]


def fit_window_size(width: int, height: int) -> tuple[int, int]:
    """Fit the requested window size into the allowed limits.

    Fit, not reject, and that is the main point here. The size comes from the page, which
    computes it from the shape of the arrived frame: its own width, and the height by
    proportion. The shapes can be anything (a wide chat strip, a narrow column), and a
    refusal by absolute pixels would act on them as a refusal outright. The page got "no"
    three times, decided there was no such route, and stopped asking until the end of the
    session. The window stayed unfitted, and a frameless window has no means of being fixed
    by hand.

    We keep the proportion while it fits in the limits: we scale as a whole rather than cut
    the sides separately. If it does not fit at all, we clamp the sides separately: the
    frame in the window is `cover` anyway, and it will crop the excess itself.
    """
    min_w, min_h, max_w, max_h = WINDOW_BOX
    width = max(1, int(width))
    height = max(1, int(height))
    scale = max(1.0, min_w / width, min_h / height)
    scale = min(scale, max_w / width, max_h / height)
    fitted_w = max(min_w, min(max_w, round(width * scale)))
    fitted_h = max(min_h, min(max_h, round(height * scale)))
    return fitted_w, fitted_h


def clamp_window_size(
    width: int, height: int, min_width: int | None = None
) -> tuple[int, int]:
    """Squeeze the size into the limits for each side separately, without the proportion.

    This is for the handle in the window corner, not for fitting to a frame. Where
    `fit_window_size` keeps the proportion, this function must not: the handle is dragged
    as it is, moving the mouse diagonally with no thought of shape. And as soon as the
    height hit its limit, the same proportion started to grow the width by itself, without
    any press on it. From outside it looked as if the window suddenly decided to expand
    sideways in the middle of dragging by the corner. Here the excess is simply cut along
    its own axis, with no scaling of the whole.

    `min_width` raises the lower bound above `WINDOW_BOX` where the panel itself has its own,
    higher floor. For now this is only the "all at once" window with the screen
    panel open (`LIVE_DRAWER_MIN_WIDTH`). It never lowers the bound below the base, only
    raises it: a closed panel and the other panels do not use it at all.
    """
    min_w, min_h, max_w, max_h = WINDOW_BOX
    min_w = max(min_w, min_width or 0)
    return max(min_w, min(max_w, int(width))), max(min_h, min(max_h, int(height)))


def _speech_json(e: SpeechEvent) -> dict:
    return {
        "t": e.t / 1e9,
        "source": e.source.value,
        "text": e.text,
        "error": e.error,
        "confidence": e.confidence,
        # Both engines get the same slicing from VAD, so their `t` matches exactly, and the page
        # uses it to place the utterances opposite each other. It is a key for gluing, not just a
        # timestamp.
        "engine": e.engine,
        "latency_ms": round(e.latency_ms) if e.latency_ms is not None else None,
    }


# A cap on the size of a pasted photo. The page compresses it itself (by the long side and
# by quality), so real photos fit in half a megabyte. The cap is protection against an
# accidental huge file: it would go over the network to the account server and cost
# tokens.
MAX_PASTED_IMAGE_BYTES = 6 * 1024 * 1024


def _pasted_image(value) -> bytes | None:
    """A photo from the question line: base64 -> JPEG bytes. `None` means no photo.

    Providers declare the image as `image/jpeg`, and accepting a PNG here would mean sending
    the model bytes of the wrong format. The page always re-compresses to JPEG, so anything
    that is not JPEG is an error, not a reason to silently break the request.
    """
    if not value:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="the photo doesn't fit")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="the photo couldn't be read") from None
    if len(data) > MAX_PASTED_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="the photo is too large")
    if not data.startswith(b"\xff\xd8\xff"):
        raise HTTPException(status_code=400, detail="the photo doesn't fit")
    return data


def _question_of(payload: dict | None) -> str | None:
    """The question from the request body: text or nothing.

    A list or a number instead of a string used to fail deep inside command parsing and
    answer with a 500. Hotkeys and foreign clients hit this route not only from the page.
    """
    value = (payload or {}).get("question")
    if value and not isinstance(value, str):
        raise HTTPException(status_code=400, detail="the question must be text")
    return value or None


def _answer_json(a: Answer) -> dict:
    return {
        "text": a.text,
        "latency_ms": round(a.latency_ms),
        # We do not give the page the model that answered: the person is not shown it, and the
        # app's replies contain no names of models and services
        # (`tests/test_user_text_is_plain.py`).
        "error": a.error,
        "trigger": a.meta.get("trigger", ""),
        "reason": a.meta.get("reason", ""),
        # The utterance that the question was asked because of. In the card it stands in place of
        # the filter's words: "the other party asked a question" does not say which question.
        "cause": a.meta.get("cause", ""),
        "image_kb": a.meta.get("image_kb", 0),
        # Where the picture came from: "screen" is a screenshot, "paste" is a photo pasted by the
        # person. The label on the card says what is really the case.
        "image_src": a.meta.get("image_src", "screen"),
        # What actually went to the model (`GET /answer/<number>/prompt`) is not here: up to twelve
        # thousand characters for each of the replies, twice a second to each window, and the page
        # does not read this field.
        # Whether "Think longer" was requested: the tag in the reply card is
        # drawn from this field, not from the model. The stub and the command-line provider do not
        # change the model at all, but the person pressed the button anyway.
        "deep": bool(a.meta.get("deep", False)),
        # A growing list: voice-over by sentences, with the ready chunks appearing one at a time;
        # see `tts/reply.py`. Empty if voice-over is off or has not yet started synthesis. The
        # core knows nothing about TTS except this field.
        "audio_ids": a.meta.get("audio_ids", []),
    }


def load_admin():
    """Find the channel's admin page, if the branch supplied one.

    It is looked up by module name, not registered in a shared place. The reason is how the
    project is laid out: the core is in master, the channels are in three branches, and
    edits flow by merge from master. A shared list of channels in a shared file would mean
    a conflict on every merge, with three branches editing one line. Here master simply does
    not find `argus.ui.admin`, and each branch finds its own.
    """
    try:
        from argus.ui.admin import build_panel
    except ImportError:
        return None
    return build_panel


def has_admin(cfg=None) -> bool:
    """Whether the settings page is available.

    Two conditions, both mandatory: the branch supplied its own `ui/admin.py`, and the
    `ui.admin` setting is on. The second is a switch for the product: we do not show
    technical knobs to the user; they are edited in `argus.toml`.
    """
    if cfg is not None and not cfg.ui.admin:
        return False
    return load_admin() is not None


def snapshot(app: Argus) -> dict:
    """The full state — what is visible in the dashboard."""
    gate = app.assistant.gate if app.assistant else None
    context = render_context(
        app.timeline,
        screen_now="\n".join(app.state.screen_lines),
        max_chars=app.cfg.brain.max_context_chars,
    )
    return {
        "levels": {
            src.value: {"rms": lv.rms, "peak": lv.peak}
            for src, lv in app.state.levels.items()
        },
        # By the start time of the utterance, not by the time of arrival.
        #
        # These are different orders. Utterances accumulate in the queue as recognition hands them
        # over, and it spends different amounts of time on different pieces: a short phrase from
        # the other party overtakes a long phrase of one's own that was said earlier. In the
        # timeline this looks like a reply that stands before its question.
        #
        # We sort here, not on the page: the order is the same for everyone who reads the
        # snapshot, and it can be checked by a test only here.
        "speech": [_speech_json(e) for e in sorted(app.state.speech, key=lambda e: e.t)],
        "speech_b": [_speech_json(e) for e in sorted(app.state.speech_b, key=lambda e: e.t)],
        # Utterances from earlier visits to this session: the page draws them above the live ones
        # (wall-clock time).
        "past_speech": app.state.past_speech,
        # Engine names for the column headings. An empty `b` means the comparison is off, and the
        # page shows a single column, as before.
        "engines": {
            "a": app.cfg.stt.provider,
            "b": (
                app.speech.shadow.name
                if getattr(app, "speech", None) is not None and app.speech.shadow
                else ""
            ),
        },
        "answers": [_answer_json(a) for a in app.state.answers],
        # A request that is in progress right now. There is no reply yet, but it must be known at
        # once that one is on its way and which utterance it relates to. Without this, a pause of
        # three seconds is indistinguishable from "the filter stayed silent", and the person
        # presses the button on top of an already running request.
        "thinking": (
            {**app.assistant.pending, "t": app.assistant.pending["t"] / 1e9}
            if app.assistant is not None and app.assistant.pending
            else None
        ),
        "screen": {
            "lines": app.state.screen_lines,
            "t": app.state.screen_t,
            "window": app.state.screen_window,
            # What of the read text really went to the model. Everything else is a repeat of the
            # previous screen and does not get into the prompt.
            "new": app.timeline.digest.last_new,
        },
        # Frames that went to the model, for the conversation timeline. We do not hand over the
        # bytes themselves: the snapshot flies over the websocket twice a second, and putting
        # hundreds of kilobytes per frame into it would clog the channel with pictures that are
        # almost always the same. Here there is only a timestamp and a weight, and the picture
        # itself is pulled by a link and cached by the browser.
        # The server can accept a photo pasted into the question line. The page is read live from
        # `src`, while the server in the built program is the one that was built. Without this
        # flag the page would show a thumbnail, and an old server would ignore the `image_b64`
        # field: the paste would look as if it worked, but the photo would not reach the model.
        "can_paste_image": True,
        "shots": [
            {"t": sh.t / 1e9, "id": sh.t, "kb": sh.kb, "kind": sh.kind}
            for sh in app.state.shots
        ],
        "frames": app.state.frames,
        "stages": {
            key: {"name": st.name, "last": round(st.last_ms), "avg": round(st.avg_ms),
                  "count": st.count}
            for key, st in app.state.stages.items()
        },
        "gate": gate.stats() if gate else {},
        "gate_reason": (
            gate.last_reason if gate and hasattr(gate, "last_reason") else ""
        ),
        "last_decision": (
            app.assistant.last_decision.reason
            if app.assistant and app.assistant.last_decision
            else ""
        ),
        "timeline": {"entries": len(app.timeline), "tokens": estimate_tokens(context)},
        # Which chat the app is in now: the working screen writes its name in the header. `None`
        # means none: the home page.
        "chat": chats_of(app).brief() if chats_of(app) is not None else None,
        "devices": {
            "mic": app.mic.name if app.mic else "",
            "loopback": app.loopback.name if app.loopback else "",
        },
        "controls": app.controls(),
        # Pipeline complaints, key combinations that did not register, and bad settings, all
        # together: for the person this is one question, what is not working now. A combination
        # taken by another program breaks the quietest: the setting is in place, the key is
        # pressed, and nothing happens. It is the same with settings: one wrong field is silently
        # replaced by the ordinary value, and without this line the reply would come out not as
        # the person chose, with nowhere to look for the cause.
        "problems": app.problems() + _hotkey_problems() + assist_settings.problems(app.cfg),
        # Among the complaints there is one about sound: only then does the page show
        # "Reconnect audio" (`index.html`). Without the flag the button
        # appeared next to any complaint, although a taken combination and bad settings are not
        # cured by it.
        "audio_problem": _audio_problem(app),
        "healthy": app.healthy,
        # Whether this copy has an admin page. master has none: there is nothing to configure for
        # a channel that does not exist. The link in the header is drawn according to the fact,
        # not always: a dead link is worse than a missing one.
        "admin": has_admin(app.cfg),
        # Why recognition cannot be turned on, on every snapshot and not only in reply to a click
        # on the button (`/control`). The difference matters. Before the first click, or right
        # after a reconnect (the app window reopened, the websocket forwarded a new snapshot),
        # without this field the person would not learn what is in the way until they press the
        # button themselves, and on a freshly downloaded build that is the first thing they press.
        "account_hint": _no_right(app, soon=True),
        # Recognition was requested, and the person has not yet seen the recording notice: the
        # window must show it. This is needed for those who turned it on with a key combination:
        # `/control` does not return a reply to the page for them.
        "notice_ask": app.cfg.legal.notice and legal.acks_of(app).asking(),
        # Whether capture is open. In settings mode (`argus run --settings`) the pipeline is
        # assembled but not started: there are no devices and no weights. The live numbers are
        # honest zeros then, and without this flag the page would show them as a measurement
        # ("level zero", "latency zero"), that is, it would lie silently.
        "capture": app.mic is not None,
        "frame_gate": (
            {
                "seen": app.vision.gate.seen,
                "passed": app.vision.gate.passed,
                "rate": round(app.vision.gate.pass_rate * 100),
            }
            if app.vision
            else {}
        ),
    }


def _hotkey_problems() -> list[str]:
    """Key combinations the system refused to hand over. There are none in the browser."""
    from argus.desktop import hotkeys

    keys = hotkeys()
    return list(keys.problems) if keys is not None else []


def _audio_problem(app: Argus) -> bool:
    """Whether the sound itself complains about something: for the "Reconnect audio"
    button on the page.

    The button fixes microphone and system-sound capture and nothing else. Complaints about
    the screen, recording, recognition, key combinations, and settings are not cured by a
    reconnect, and a button next to them would promise what it will not do.

    The conditions are the same ones by which `Argus.problems()` puts lines about sound into
    its list: the stream is not alive, or the sound complains itself (`complaints()`: device
    not found, listening to the wrong one, microphone silent). We do not guess from the text
    of the lines: the words live in `capture/audio.py` and `mac/audio.py` and change, and a
    flag would then break silently, with the button vanishing or appearing out of place.
    """
    audio = app.audio
    return not audio.healthy or bool(audio.complaints())


def _no_right(app: Argus, soon: bool = False) -> str:
    """Why recognition cannot be turned on. Empty means it can.

    `soon=True` is for the state snapshot, which is assembled twice a second on the event
    loop: it takes the last known account state without waiting for the network
    (`Account.state_soon`). A real poll of the server (`soon=False`) is a request of a
    hundred milliseconds, and on a bad connection of seconds, and in the snapshot it would
    hold the whole server: windows would respond in jerks, and "thinking" and new frames
    would arrive late.

    This is politeness, not protection. The real turnstile is the server refusing to hand
    over the model's reply; here we only keep the person from pressing the button and
    waiting for replies that will not come. A check inside a program that sits on someone
    else's computer is cut out in ten minutes and cannot be relied on.

    If the account server is not configured, we do not interfere at all: that is a build of
    one's own on one's own keys, and it has no accounting.
    """
    from argus.ui.admin import account_of

    account = account_of(app)
    if account is None:
        return ""
    state = account.state_soon() if soon else account.state()
    if not state.get("signed_in"):
        return "you need to sign in — it's in settings"
    # No connection to the server: let them in. Refusing because of our own network hiccup
    # would stop the conversation for a person who has paid for it.
    if state.get("offline"):
        return ""
    right = state.get("right") or {}
    if right.get("allowed"):
        return ""
    return right.get("reason") or "recognition unavailable"


# How many windows can listen to the state at once: the main one, three detached ones, and
# a couple of browser tabs for debugging. More than that is no longer app windows but pages
# opening sockets one after another: for each of them the whole snapshot is assembled and
# compressed twice a second.
MAX_CLIENTS = 16

# How long to wait for one window to accept the snapshot. A hung or minimized window must
# not hold up the others: it is disconnected, and the page reconnects by itself.
SEND_TIMEOUT_S = 2.0


async def push(clients: set, payload: str, timeout: float = SEND_TIMEOUT_S) -> None:
    """Send the snapshot to all windows at once; disconnect one that did not accept it in time
    or failed.

    The walk is over a copy. While one window accepts the snapshot, another manages to
    connect or close, and iterating the live set would fail with "Set changed size during
    iteration". The broadcast itself would die with it, without a single line in the log.
    """

    async def one(ws) -> None:
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout)
        except Exception:  # noqa: BLE001 — the tab was closed, the window hung, or the channel is clogged
            clients.discard(ws)
            try:
                await asyncio.wait_for(ws.close(), 1.0)
            except Exception:
                log.debug("window already closed", exc_info=True)

    await asyncio.gather(*(one(ws) for ws in list(clients)))


async def supervise(name: str, start, pause: float = 1.0) -> None:
    """Keep a background task alive: if it fails, write it to the log and start again after
    `pause`.

    The state broadcast and the chat-history writing used a bare `create_task`. The first
    exception killed the task silently (the log had at most "never retrieved" on exit), and
    all windows stopped updating until a restart. `start` gives a new coroutine on each
    launch.
    """
    while True:
        try:
            await start()
            return                      # finished by itself, like the update check without a server
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background task '%s' crashed — restarting", name)
            await asyncio.sleep(pause)


def build(app: Argus, trusted_hosts: Sequence[str] = ()) -> FastAPI:
    """Build the web app around the running pipeline.

    `trusted_hosts` are names beyond `127.0.0.1`/`localhost`/`[::1]` and `ui.host` under
    which the server may be opened (`ui/guard.py`: a foreign `Host` is rejected).

    The FastAPI imports are at the top of the module, not here, and this matters. The module
    has `from __future__ import annotations` on, which turns all annotations into strings.
    FastAPI resolves them through the module's **global** names, and with an import inside
    a function it does not find `WebSocket`, and then closes the connection instead of
    accepting it. There is no error and no warning: the page just stays empty forever.
    """
    clients: set[WebSocket] = set()
    latest_frame: dict[str, bytes] = {}
    # Reopening the sound runs in threads (`/control`), and two reopenings in a row (a double
    # click on the button, or a microphone change while a reconnect is in progress) would go
    # through at the same time: the `close()` of one and the `open()` of the other would bring
    # the capture threads up twice. Before, the event loop itself lined them up, since it
    # stood on each of them; now this lock does it.
    audio_lock = asyncio.Lock()
    chats = manager_of(app)
    # A new version while the app is running: the "Update" button in the window
    # (`update_service.py`).
    updater = UpdateService(app.cfg, running=lambda: bool(app.running))

    async def broadcast() -> None:
        """Every half second, broadcast the state to all open tabs."""
        while True:
            await asyncio.sleep(0.5)
            if not clients:
                continue
            try:
                payload = json.dumps(snapshot(app), ensure_ascii=False)
            except Exception:
                # One bad snapshot must not stop the broadcast.
                log.exception("state snapshot could not be built")
                continue
            await push(clients, payload)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks = [
            asyncio.create_task(supervise("state broadcast", broadcast)),
            # The history of the open chat is written by a separate task: it listens to the bus the
            # way the page listens to the state, and interferes with neither.
            asyncio.create_task(supervise("chat history", chats.run)),
            asyncio.create_task(supervise("update check", lambda: updater.run())),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

    # The /docs and /redoc pages pulled scripts from a foreign CDN (jsdelivr) right into the
    # app's address, and nobody needs them.
    web = FastAPI(title="Argus", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # Interface language: the dictionary and the translation are inserted into all HTML pages
    # by a single layer, not in every route (see `ui/i18n.py`).
    i18n.install(web, app)

    # The shared design system: /design/argus.css, /design/panel.js, /design/specimen.html.
    # The `argus.design` package belongs to master; the branch only reads and links it.
    design.mount(web)

    build_panel = load_admin() if app.cfg.ui.admin else None
    if build_panel is not None:
        try:
            build_panel(app).attach(web)
        except Exception:
            # The gauge is needed even without the admin page: seeing what happens in the pipeline is
            # more important than turning settings, and the first must not be lost because of the
            # second. The failure goes to the log in full, not swallowed.
            log.exception("admin panel did not start — the dashboard works without it")

    attach_chats(web, app, chats)
    attach_update(web, updater)

    # The conversation recap and the tracked topics (`argus/recap/`) have their own routes,
    # like the chats.
    from argus.recap.routes import attach as attach_recap

    attach_recap(web, app)

    @web.get("/panel/{name}", response_class=HTMLResponse)
    async def panel_page(name: str) -> Response:
        """One panel as a separate page, for detaching into its own window.

        The same `index.html` is served, not separate markup for each panel, and this is the
        main decision here. There are two panels (plus their combination, "live"), they hold a
        dozen-plus live blocks, and a second copy of the rendering would diverge from the first
        on the first edit. It would diverge silently, too: both look right and show different
        things.

        The page itself understands from its own address that it is opened in single view, and
        hides everything extra. The data are the same, from the same websocket.
        """
        if name not in PANELS:
            return Response(status_code=404)
        return HTMLResponse(
            (STATIC / "index.html").read_text(encoding="utf-8"),
            headers=page_headers(),
        )

    def detached():
        """The windows of detached panels, or None if we are in a browser tab.

        We ask `desktop` at request time, not when the server is built: the server comes up
        before the window, and at startup the answer would be "no windows" forever. The import
        is inside the function because the module pulls in WebView, which a browser tab does not
        need.
        """
        from argus.desktop import panels

        return panels()

    @web.post("/window/open")
    async def window_open(payload: dict) -> dict:
        """Detach a panel into a separate window on top of the screen.

        Only the app process itself can create its own OS window (`webview.create_window`); a
        browser tab cannot be given that. Instead of refusing and silently doing nothing, the
        page gets `reason: "browser"` here. By it, JS opens the panel as an ordinary browser
        popup window (`window.open`, in `openPanel`, index.html). It is not the same window as
        the app's (it has no minimum size of its own and no hiding from screen capture, which
        a browser cannot do itself), but it has the same address and the same look, as a
        separate window on top of the tab, instead of silence in answer to a click.
        """
        name = str(payload.get("panel") or "")
        if name not in PANELS:
            return {"ok": False, "error": f"no such panel: {name}"}
        board = detached()
        if board is None:
            return {
                "ok": False,
                "reason": "browser",
                "error": "detaching works only in the app — a browser tab "
                         "has no separate windows",
            }
        try:
            # In a separate thread: a window is not born instantly, and the page is meanwhile waiting
            # for data from the same server. Waiting for the window on the event loop would freeze the
            # other panels too.
            await asyncio.to_thread(board.open, name)
        except Exception as e:  # a window must not take the server down
            log.exception("could not detach the panel")
            return {"ok": False, "error": friendly.plain_or(e, "failed")}
        return {"ok": True}

    @web.post("/window/close")
    async def window_close(payload: dict) -> dict:
        """Close a detached window.

        The cross on the panel goes here, not to `window.close()`: the window has no frame, and
        a panel that did not close would stay on screen with no way to remove it. This is also
        the only way out of the app when the main window is already closed: the process lives
        as long as the last window is alive.
        """
        name = str(payload.get("panel") or "")
        board = detached()
        if board is None:
            return {"ok": False, "error": "the window only exists in the app"}
        try:
            # In a separate thread, like opening: closing a window goes through the WebView event loop
            # and does not return instantly, and meanwhile the same server feeds data to the other
            # panels.
            await asyncio.to_thread(board.close, name)
        except Exception as e:
            log.exception("could not close the panel")
            return {"ok": False, "error": friendly.plain_or(e, "failed")}
        return {"ok": True}

    @web.post("/window/main")
    async def window_main(payload: dict) -> dict:
        """The strip of the main window: minimize, maximize, close, drag.

        Windows took off the system strip (`desktop.use_page_caption`), and the page draws the
        buttons (`winbar.js`). Python moves and resizes the window: the page sees the mouse in
        logical pixels, while the window lives in physical ones. In a browser tab there is no
        main window, and the reply `ok: false` does nothing.
        """
        action = str(payload.get("action") or "state")
        if action not in MAIN_WINDOW_ACTIONS:
            return {"ok": False, "error": f"unknown action: {action}"}
        from argus.desktop import main_window_action

        return await asyncio.to_thread(main_window_action, action)

    @web.post("/window/resize")
    async def window_resize(payload: dict) -> dict:
        """The size of a detached window.

        Two callers, two ways to squeeze into the limits. The screen window asks for it when the
        first frame arrives: the frame fits into the window whole, and while the shapes do not
        match, margins remain at the edges, so here we keep the proportion (`fit_window_size`).
        The handle in the corner pulls the window as it is, with no shape behind it, and the
        same proportion has nowhere to come from there: `free` in the request asks for
        `clamp_window_size`, which cuts the sides separately. Which of the two applies is
        decided by whoever sent the request, not by the panel name: both call the same route,
        and telling them apart would have to come from the page somewhere anyway.

        The third, optional key, `screenOpen`, comes only from the arrow that opens the screen
        panel in the "all at once" window, and it means not a size but a change of
        floor. While the panel is open, the window has its own, higher limit
        (`LIVE_DRAWER_MIN_WIDTH`). We keep it on the window itself (`Panels.set_min_width`), not
        only in this request, so that a corner handle dragged later with no `screenOpen` in the
        request also cannot take the window below it while the panel is still open.

        The fourth, also optional, `seq` is a mark of the moment of sending (JS time,
        `Date.now()` on the page), not a sequential number. This route is called independently
        by the corner handle, the "show screen" arrow, and the periodic fitting of the screen
        window to the frame shape (`fitScreenWindow` in index.html). Each call goes to
        `asyncio.to_thread` on the shared thread pool, that is, into a thread of its own, and
        they may finish in a different order than they were sent. Without the mark, an older
        request that reached the thread later than a newer one would quietly override its
        result. `SetWindowPos` does not remember that it was already called with a fresher size,
        and both replies would return `ok: True`, without a hint that the size had rolled back.
        When a mark is present, `Panels.resize` does not apply a request that is older than the
        one already applied. That check lies there, not here: applying and comparing must be
        one atomic action, not two separate ones (otherwise the race would just move one step
        earlier, between the comparison in this function and the call in that one).

        One more caller is the question line in the "answers" and "all at once"
        windows (index.html, `soloBar`). When the mouse is over the window, it grows the
        window downward by its own height, and it does so not in one request but in a dozen in a
        row, one per frame, with `free: true`, a fresh `seq` mark, and a height that grows or
        shrinks along a curve, so the window slides out smoothly. It needs the same reply as the
        handle: `ok` and the applied `width`/`height`, by which it tells a clipped size (the
        limit from above) from an executed one.
        """
        name = str(payload.get("panel") or "")
        screen_open = payload.get("screenOpen")
        seq = payload.get("seq")
        try:
            asked_width = int(payload.get("width") or 0)
            asked_height = int(payload.get("height") or 0)
        except (TypeError, ValueError, OverflowError):   # "Infinity" in JSON is a number for Python
            raise HTTPException(status_code=400, detail="the size must be numbers") from None
        board = detached()
        if board is None:
            return {"ok": False, "error": "resizing is only available in the app"}
        if screen_open is not None:
            min_width = LIVE_DRAWER_MIN_WIDTH if (name == "live" and screen_open) else None
            await asyncio.to_thread(board.set_min_width, name, min_width)
        if payload.get("free"):
            width, height = clamp_window_size(asked_width, asked_height, board.get_min_width(name))
        else:
            width, height = fit_window_size(asked_width, asked_height)
        try:
            applied = await asyncio.to_thread(board.resize, name, width, height, seq)
        except Exception as e:
            log.exception("could not resize the window")
            return {"ok": False, "error": friendly.plain_or(e, "failed")}
        # The panels may have been hidden by the "panic" action (the size is remembered but not
        # applied), or the request turned out to be stale next to a newer one (see `seq` in the
        # docstring above and `Panels.resize`). Saying "done" in the first case is not allowed:
        # the page would stop asking. In the second case it is not an error at all but a normal
        # lost race, yet `ok: True` would mean "applied" for it, and that is untrue.
        if not applied:
            return {"ok": False, "error": "panel is hidden or the size is outdated — not applied"}
        return {"ok": True, "width": width, "height": height}

    @web.get("/window/capture")
    async def window_capture_state() -> dict:
        """Whether the application's windows are currently hidden from screen capture."""
        from argus.desktop import capture_excluded, main_window

        return {"available": main_window() is not None, "excluded": capture_excluded()}

    @web.post("/window/capture")
    async def window_capture(payload: dict) -> dict:
        """Turn the invisibility of the app's windows in screen recording on or off. The window
        stays visible to the person themselves in any case: it hides not from eyes but from
        `SetWindowDisplayAffinity`; see `desktop.hide_from_capture`.

        Only an explicit `excluded: true|false`. A "toggle blindly" mode used to be here, and an
        empty POST from a foreign page made the windows visible in screen sharing; the settings
        page always sends the value.
        """
        from argus.desktop import capture_excluded, main_window, set_capture_exclusion

        excluded = payload.get("excluded")
        if not isinstance(excluded, bool):
            raise HTTPException(status_code=400, detail="you need to say explicitly whether to hide the windows")
        if main_window() is None:
            return {"ok": False, "error": "only works in the app"}
        # In a thread: without a window the switch waits for it for up to five seconds
        # (`hide_from_capture`), and all that time the server's event loop would stand still,
        # together with the replies to the other pages.
        ok, detail = await asyncio.to_thread(set_capture_exclusion, excluded)
        return {"ok": ok, "excluded": capture_excluded(), "detail": detail}

    @web.get("/chat/{chat_id}", response_class=HTMLResponse)
    async def chat_page(chat_id: str) -> Response:
        """The chat's working screen: the same gauge that used to be at `/`.

        The chat is opened right here, before the page is served, and not by a separate request
        from the page. The page arrives already with its timeline and settings, and a bookmark
        to a chat address, opened after the app restarted, works the same as navigation from the
        home page. An already open chat is not reset in the process: a page refresh must not
        erase the conversation.

        Working with already recorded text is a separate screen, "Work with the recording"
        (`/chat/<id>/record`, `argus/recap/`). A button of its own in the session row on the
        home page leads to it, while this address always opens the working screen.
        """
        if not valid_id(chat_id):
            return RedirectResponse("/", status_code=302)
        try:
            await chats.activate(chat_id)
        except KeyError:
            return RedirectResponse("/", status_code=302)
        except Exception:
            # We serve the page anyway: an empty screen without explanation is worse than a chat where
            # something did not apply, which is visible in the log.
            log.exception("chat did not open properly")
        return HTMLResponse(
            (STATIC / "index.html").read_text(encoding="utf-8"),
            headers=page_headers(),
        )

    @web.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        """The home page is a list of chats. It is read from disk and **not cached**.

        The browser kept an old version after edits, and the person saw buttons that were no
        longer in the code. Debugging that is impossible: the file says one thing and the screen
        another.

        A cache is not enough to explain it: the same symptom comes from the **built `.exe`**,
        which has its own copy of `static/` and the same port 8420. So the response has an
        `X-Argus-Build` header: whether it is a build or the sources, and when the page changed.
        The answer to "is what I see current" must come from a single `curl -I`, not from a day
        of investigation.
        """
        return HTMLResponse(
            (STATIC / "home.html").read_text(encoding="utf-8"),
            headers=page_headers(),
        )

    @web.get("/build")
    async def build_info() -> dict:
        """Where the page came from. Details are in `build_identity`."""
        return build_identity()

    @web.get("/p/{name}")
    async def page(name: str) -> Response:
        """A channel page by file name: /p/argus.css, /p/panel.js.

        A generic route, rather than one of its own for each branch, and it removes the one
        unavoidable conflict of the plan: `ui/server.py` belongs to master, while the channel
        pages belong to the branches. With it, a branch adds a page by **dropping in a file**
        and touches not a single line of Python. The price is the address `/p/live` instead of
        `/live`; it was paid deliberately.

        The headers are the same as for the gauge, for the same reason: the browser kept an old
        version, and the person saw buttons that are no longer in the code.
        """
        if not re.fullmatch(r"[a-z0-9_-]+(\.css)?", name):
            return Response(status_code=404)
        path = STATIC / (name if name.endswith(".css") else f"{name}.html")
        if not path.exists():
            return Response(status_code=404)
        media = "text/css" if name.endswith(".css") else "text/html; charset=utf-8"
        return Response(
            content=path.read_text(encoding="utf-8"),
            media_type=media,
            headers=NO_CACHE,
        )

    @web.get("/chrome.css")
    async def chrome_css() -> Response:
        """The shared part of the styling: header, transitions, buttons.

        One for the gauge and for the settings. Before, each page had its own rules, and they
        diverged not by design but by oversight: one page had a gray header, the other a header
        with a channel strip; one had transitions as links, the other as a group. A person
        moves between them ten times per settings change and sees two different programs.
        """
        return Response(
            (STATIC / "chrome.css").read_text(encoding="utf-8"),
            media_type="text/css; charset=utf-8",
            headers=NO_CACHE,
        )

    @web.get("/chrome.js")
    async def chrome_js() -> Response:
        """The shared behavior of the header: the menu behind three bars.

        One for the gauge and for the settings, for the same reason as `chrome.css`: the pages
        have their own markup, but the shared node is one, `nav.tabs`, and keeping two copies of
        one menu means that one day they will drift apart.
        """
        return Response(
            (STATIC / "chrome.js").read_text(encoding="utf-8"),
            media_type="application/javascript; charset=utf-8",
            headers=NO_CACHE,
        )

    @web.get("/winbar.js")
    async def winbar_js() -> Response:
        """The strip of the app window: an icon on the left, three buttons on the right.

        It is drawn only in the app's main window (the `desk=1` mark in the address), so there
        is one file for all the pages where that window opens.
        """
        return Response(
            (STATIC / "winbar.js").read_text(encoding="utf-8"),
            media_type="application/javascript; charset=utf-8",
            headers=NO_CACHE,
        )

    @web.get("/update-button.js")
    async def update_button_js() -> Response:
        """The "Update" button — bright, at the top, on every page of the application
        (`update_service.py`).
        """
        return Response(
            (STATIC / "update-button.js").read_text(encoding="utf-8"),
            media_type="application/javascript; charset=utf-8",
            headers=NO_CACHE,
        )

    @web.get("/account.js")
    async def account_js() -> Response:
        """The account block: one markup for the slide-out menu and for the settings.

        The same argument as for `chrome.js`, only stronger: the block has six states, and a
        second markup for it would diverge from the first immediately. They have nowhere to
        diverge: it is one window shown in two places.
        """
        return Response(
            (STATIC / "account.js").read_text(encoding="utf-8"),
            media_type="application/javascript; charset=utf-8",
            headers=NO_CACHE,
        )

    @web.get("/vendor/temml/temml.min.js")
    async def temml_js() -> Response:
        """Rendering of formulas in replies: LaTeX -> MathML.

        It is bundled into the project rather than loaded from a CDN: the page lives without a
        network.
        """
        return Response(
            (STATIC / "vendor/temml/temml.min.js").read_bytes(),
            media_type="application/javascript; charset=utf-8",
            headers=NO_CACHE,
        )

    @web.get("/vendor/temml/Temml-Local.min.css")
    async def temml_css() -> Response:
        """Formula style: local fonts, without the Temml web fonts."""
        return Response(
            (STATIC / "vendor/temml/Temml-Local.min.css").read_bytes(),
            media_type="text/css; charset=utf-8",
            headers=NO_CACHE,
        )

    @web.get("/vendor/temml/Temml.woff2")
    async def temml_font() -> Response:
        """The only font that the CSS above still asks for.

        Handwritten capital letters (𝒜, ℬ...): they are not in ordinary system fonts, and
        everything else is drawn with what is already installed in the system.
        """
        return Response(
            (STATIC / "vendor/temml/Temml.woff2").read_bytes(),
            media_type="font/woff2",
            headers=NO_CACHE,
        )

    @web.get("/screen.jpg")
    async def screen_jpg() -> Response:
        """A picture of what goes to recognition.

        Exactly the reading area, not the whole monitor: before, with a window selected, the
        preview showed the whole screen and did not match what got into the timeline. We encode
        on request: doing it constantly is an extra 5 ms per frame.
        """
        import cv2

        if app.screen is None:
            return Response(status_code=404)
        frame = await asyncio.to_thread(app.preview_image)
        if frame is None:
            return Response(content=latest_frame.get("jpg", b""), media_type="image/jpeg")

        # We shrink it: in the dashboard the picture is a third of the screen wide, the full
        # resolution would not fit there anyway, and it weighs four times as much.
        if frame.shape[1] > 900:
            scale = 900 / frame.shape[1]
            frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 62])
        if not ok:
            return Response(status_code=500)
        latest_frame["jpg"] = buf.tobytes()
        return Response(content=latest_frame["jpg"], media_type="image/jpeg")

    @web.get("/screen.mjpg")
    async def screen_mjpg() -> StreamingResponse:
        """A continuous stream of frames of the reading area.

        Before, the preview was refreshed by polling: the browser asked for a new picture every
        N milliseconds. It looked like a slideshow, and polling more often means breeding
        requests. MJPEG solves it differently: one connection into which the server sends the
        frames itself, and the browser plays them in an ordinary `<img>` like a video.

        Encoding goes to a separate thread: 4 ms per frame in the shared loop at 12 frames per
        second would take a noticeable share of time away from recognition.
        """
        import cv2

        boundary = "argusframe"

        def grab_and_encode():
            """Grab a frame and compress it in a single visit to a thread.

            Before, these were two separate hops between threads per frame. The work itself costs
            2.8 ms, and the overhead of the hops was comparable to it.
            """
            img = app.preview_image()
            if img is None:
                return None
            if img.shape[1] > 900:
                scale = 900 / img.shape[1]
                img = cv2.resize(img, None, fx=scale, fy=scale,
                                 interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
            return buf.tobytes() if ok else None

        async def frames():
            delay = 1.0 / max(1, app.cfg.screen.preview_fps)
            while True:
                if not app.sources.get("screen", True):
                    await asyncio.sleep(0.3)
                    continue
                try:
                    jpeg = await asyncio.to_thread(grab_and_encode)
                except RuntimeError:
                    # The thread pool has been shut down: the app is being closed. This is the end of work,
                    # not a breakage, and a traceback must not be printed here: a closed window would look
                    # crashed. We have twice already concluded exactly that, looking at this very output.
                    return
                if jpeg is None:
                    await asyncio.sleep(0.3)
                    continue
                if jpeg:
                    head = (
                        f"--{boundary}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n"
                    )
                    yield head.encode() + jpeg + b"\r\n"
                await asyncio.sleep(delay)

        return StreamingResponse(
            frames(),
            media_type=f"multipart/x-mixed-replace; boundary={boundary}",
            headers={"Cache-Control": "no-store"},
        )

    @web.post("/legal/notice")
    async def legal_notice() -> dict:
        """"Got it" in the notice window: remember it and do not ask again."""
        legal.acks_of(app).confirm_notice()
        return {"ok": True}

    @web.post("/control")
    async def control(payload: dict) -> dict:
        """The control panel: the master switch and the checkboxes per source."""
        if payload.get("running"):
            # In a thread: a real poll of the account server is a network request, and here we are on
            # the event loop.
            denied = await asyncio.to_thread(_no_right, app)
            if denied:
                # The refusal is returned together with the ordinary state of the control panel, not as an
                # error: the button must go back to "off" and show the reason, rather than leave the page
                # with a spinning indicator.
                return {**app.controls(), "denied": denied}
            # First turn-on on this computer: first the notice about what is recorded and what goes
            # outside. Like the refusal, it is returned together with the control panel state: the
            # button goes back to "off", and the page shows the window and, after "Got it",
            # sends the turn-on again.
            if app.cfg.legal.notice and not legal.acks_of(app).notice_done():
                legal.acks_of(app).ask()
                return {**app.controls(), "notice": True}
        if "running" in payload:
            was = app.running
            # In a thread: turning recording on walks the sessions folder and cleans up old ones, and
            # turning it off appends the frames and the passport, up to ten seconds. The event loop
            # (windows, replies, hotkeys) would stand still all that time. `set_running` itself is
            # designed for any thread (the recording lock in app.py).
            await asyncio.to_thread(app.set_running, bool(payload["running"]))
            if was and not app.running:
                # Switched off by hand means the conversation is over: a summary by topics, if the topics
                # were asked to be tracked (`argus/recap/`). A chat change and a sound breakage do not get
                # here: they stop recognition not through the control panel.
                from argus.recap import recap_of

                recap_of(app).stopped()
        for name in ("mic", "loopback", "screen"):
            if name in payload:
                try:
                    app.set_source(name, bool(payload[name]))
                except KeyError:
                    pass  # the screen could have been disabled at startup
        if "target_window" in payload:
            app.set_target_window(str(payload["target_window"] or ""))
        # The three actions below reopen the sound: they wait for the capture threads to stop (up
        # to two seconds each) and bring the device up again. On the event loop this would stop
        # all the windows, replies, and hotkeys (`/audio/devices` is already in a thread for the
        # same reason).
        raw = payload.get("loopback_windows") or []
        if not isinstance(raw, list | tuple):
            raise HTTPException(status_code=400, detail="windows must be a list of names")
        async with audio_lock:
            if "loopback_windows" in payload:
                await asyncio.to_thread(app.set_loopback_windows, [str(t) for t in raw])
            if "mic_device" in payload:
                await asyncio.to_thread(
                    app.set_mic_device, str(payload["mic_device"] or "") or None)
            if payload.get("reopen_audio"):
                # Reconnecting the sound is the only action of the control panel that touches hardware. We
                # keep it here, not as a separate route: for the person it is the same kind of button on
                # the panel as the others.
                await asyncio.to_thread(app.reopen_audio)
        return app.controls()

    @web.get("/shot/{shot_id}.jpg")
    async def shot(shot_id: int) -> Response:
        """A frame from the timeline by its timestamp.

        Cached forever: the frame has already been taken and will never change, and its address
        is unique. Without this, the timeline would pull the server for every picture on every
        redraw, twice a second.

        Except for frames restored from a saved chat: they have fixed negative numbers, the same
        in every chat (`chats/manager.py`), and the address does not determine the picture. The
        window remembers the cache between launches, and in the second chat a screenshot from
        the first would show up.
        """
        found = next((sh for sh in app.state.shots if sh.t == shot_id), None)
        if found is None:
            return Response(status_code=404)
        cache = "no-store" if shot_id < 0 else "public, max-age=31536000, immutable"
        return Response(
            content=found.jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": cache},
        )

    @web.get("/audio/devices")
    async def audio_devices() -> dict:
        """The microphones that can be chosen from, and the one we are listening to now.

        Enumeration touches PortAudio, so it goes to a separate thread: in the shared loop it
        would stall the state broadcast to all the tabs.
        """
        return {
            "inputs": await asyncio.to_thread(app.audio.inputs),
            "current": app.mic.name if app.mic else "",
            "pinned": app.cfg.audio.mic_device or "",
        }

    @web.get("/windows")
    async def windows(thumbs: bool = False) -> dict:
        """The list of windows to choose from.

        Thumbnails are taken only on request: each costs 40-50 ms, and doing it in the
        background while the list is closed is a waste.
        """
        return {"windows": await asyncio.to_thread(app.windows, thumbs)}

    @web.post("/ask")
    async def ask(payload: dict) -> dict:
        # The body is required (the page and the hotkeys always send it): an empty POST without
        # `Content-Type` is the only thing a foreign page sends without the browser's permission.
        question = _question_of(payload)
        deep = bool(payload.get("deep", False))
        image = _pasted_image(payload.get("image_b64"))
        answer = await app.ask(question, deep, image=image)
        return _answer_json(answer)

    @web.post("/ask/screen")
    async def ask_screen(payload: dict) -> dict:
        """Look at the screen and ask the model.

        Separate from `/ask` because it does more: for the duration of the question it turns on
        sending the picture, even if the permanent setting is off.
        """
        question = _question_of(payload)
        deep = bool(payload.get("deep", False))
        return _answer_json(await app.ask_about_screen(question, deep))

    @web.get("/answer/{index}/prompt")
    async def answer_prompt(index: int) -> Response:
        """What went to the model in the reply with this number (as in `snapshot()["answers"]`).

        For debugging: "the model talks nonsense" and "nonsense went into the prompt" are
        different breakages, and they can be told apart only by looking into the prompt. It is
        not put into the state snapshot (`_answer_json`), so it is here, on request.
        """
        answers = list(app.state.answers)
        if not 0 <= index < len(answers):
            return Response(status_code=404)
        return Response(
            str(answers[index].meta.get("prompt", "")),
            media_type="text/plain; charset=utf-8",
            headers=NO_CACHE,
        )

    @web.get("/state")
    async def state() -> dict:
        return snapshot(app)

    @web.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        if len(clients) >= MAX_CLIENTS:
            await ws.close(code=1013)       # there are never that many app windows
            return
        await ws.accept()
        clients.add(ws)
        try:
            await ws.send_text(json.dumps(snapshot(app), ensure_ascii=False))
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(ws)

    # The guard goes in as the very last layer: a layer added later stands outside the earlier
    # ones, so a foreign request is cut off before the page language, the routes, and
    # everything else (`ui/guard.py`).
    ui = app.cfg.ui
    web.add_middleware(LocalGuard, port=ui.port, hosts=[ui.host, *trusted_hosts])
    return web


def check_websocket_support() -> None:
    """Make sure that uvicorn can do WebSocket.

    Without the `websockets` library, uvicorn does not refuse to start: it silently answers
    404 to an attempt to establish a connection. From outside this looks as if the interface
    is broken: the page opens but is empty, and the buttons do not respond. This is exactly
    how this breakage showed up, and it could be found only in a browser. So we check in
    advance and say it plainly.
    """
    try:
        import websockets  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "the websockets library is missing — the dashboard will open but stay empty. "
            "Install it: uv sync"
        ) from e


async def serve(
    app: Argus,
    host: str | None = None,
    port: int | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Bring up the dashboard. `stop` is a request to finish gracefully.

    Cancelling the task from outside is not enough: uvicorn catches the cancellation inside
    its lifespan and prints a traceback that looks like a crash. In a terminal that is
    noise, and in the app it is the only thing the person will see in the log after
    closing the window normally.
    """
    import socket

    import uvicorn

    check_websocket_support()
    bind_host = host or app.cfg.ui.host
    bind_port = port or app.cfg.ui.port
    web = build(app, trusted_hosts=[bind_host])
    config = uvicorn.Config(
        web,
        host=bind_host,
        port=bind_port,
        log_level="warning",
        access_log=False,
        # "auto" will choose an implementation itself, but if there are none it silently disables
        # WebSocket; the check above protects against that.
        ws="auto",
        # When the window is closed, the server waits for the connections to disperse, and without
        # a time limit it waits forever if one of them is endless: the screen frame stream
        # (`/screen.mjpg`) or the socket of a browser tab opened next to the window. Meanwhile the
        # process holds the single-instance lock, and a repeat launch could not start until the
        # server gave up on its own. Two seconds is enough for honest requests, and stuck ones are
        # cancelled.
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)

    # We create the listening socket ourselves rather than leaving it to asyncio inside
    # `server.serve()`: the port-binding parameters here decide who will answer the app
    # window.
    #
    # POSIX: SO_REUSEADDR, as asyncio itself does it: a port that has just been closed
    # (connections in TIME_WAIT) is taken at once, and a live foreign listener still cannot be
    # hijacked.
    #
    # Windows: SO_REUSEADDR is something quite different there. It lets a second socket onto
    # the port next to a live listener if that one also enabled it (`http.server` does so, for
    # example), and all new connections go to the first one. Argus would start without an
    # error, while the window would show the page of a foreign program, including its sign-in
    # form. Verified on this system: TIME_WAIT and leftovers of accepted connections on the
    # port do not interfere with bind() with either flag, while SO_EXCLUSIVEADDRUSE refuses if
    # anything at all already sits on the port, and does not let anyone sit down next to it
    # later. That is why it is used there.
    #
    # There is no race between two Argus instances here: only one process reaches this line,
    # and the mutex in `run_desktop` has already guaranteed that.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((bind_host, bind_port))
    except OSError as e:
        sock.close()
        if e.errno not in (errno.EADDRINUSE, errno.EACCES):
            raise
        log.error("cannot bind port %s: %s", bind_port, e)
        raise OSError("the port is taken by another program — close it and start Argus again") from e
    sock.listen(config.backlog)

    async def bye() -> None:
        await stop.wait()          # type: ignore[union-attr]
        server.should_exit = True

    closer = asyncio.create_task(bye()) if stop is not None else None
    try:
        await server.serve(sockets=[sock])
    finally:
        if closer is not None:
            closer.cancel()
