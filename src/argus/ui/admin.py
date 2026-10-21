"""Channel admin page: a helper for live conversation.

An interview, an exam, a work call, a lecture: the channel is always the same. The
person talks, the helper hears the conversation, sees the screen, and gives hints.
Nothing specifically "interview-like" is in the settings or in the prompt, and nothing
should be. What distinguishes one conversation from another is written by the person in
two fields: "about you" and "about this conversation".

There are deliberately few settings here. Everything that is turned in the middle of a
conversation (the answer format, whom to answer, what to read from the screen) lives on
the control panel, where it is a single motion rather than a form with fields. People
come here once, before the conversation, and set what does not depend on the
conversation.

That is why there are no recognition thresholds here, no screen-reading settings, and no
latency layout. They are measured and set. They should be looked at in the readout, and
turning them at random is a way to break what works.

Sound is an exception, and for a different reason. The other defaults are measured and
correct on any machine. Sound is correct only until Windows moves it by itself: plug in
a headset, and the microphone moves away. While the device is wrong, nothing works:
there are no utterances, there is nothing to answer, and the readout shows honest zeros
that look like silence in the room. This is the only place where the person has to
step in, and they step in once, before the conversation. That is exactly here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from argus.assist import gate as assist_gate
from argus.assist import materials
from argus.assist import prompt as assist_prompt
from argus.assist.settings import KEY, AssistSettings, read
from argus.brain.gate import ReplyMode
from argus.core import friendly
from argus.core.config import Config
from argus.live import live
from argus.tts import reply as tts_reply
from argus.tts.settings import KEY as TTS_KEY
from argus.tts.settings import TTSSettings, VoiceProfile
from argus.tts.settings import read as tts_read
from argus.ui.admin_base import AdminPanel, Section, Setting

if TYPE_CHECKING:
    from argus.app import Argus

log = logging.getLogger(__name__)

PAGE = live(Path(__file__).parent / "static") / "admin.html"
ACCENT = "#4fb8d8"

# A cap on one file. This is not about our abilities (we can process a hundred-megabyte
# lecture) but about the fact that a file of that size has almost always been put in the
# wrong place.
MAX_UPLOAD = 100 * 1024 * 1024

# The name of an uploaded file. We forbid what never occurs in Windows file names anyway,
# so a real file from the computer will not trip on it, but a forgery will. That means
# ":" (`a.pdf:evil.pdf` is an NTFS alternate stream: the data would land in an invisible
# place while `a.pdf` stays empty), control characters (NUL breaks the write), and
# wildcard and path characters. Also device names (`CON`, `NUL`, ...) and a trailing dot
# or space, which Windows silently drops, so the file would end up not where it is
# expected.
INVALID_NAME = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
})

# What a page sent as an image costs. The number comes from the model's documentation and
# is approximate. It is needed only so that the person sees the order of magnitude of the
# price next to the list instead of learning it from a bill.
PAGE_TOKENS = 1500

# The interface language. The same languages as for replies (`assist_prompt.LANGUAGES`:
# one list for the whole app), but instead of "same as conversation"
# it has "same as system": button labels have no conversation, but
# Windows has a language (`ui/i18n.py`).
APP_LANGUAGES = [("auto", "same as system")] + [
    (code, label) for code, label, _rule in assist_prompt.LANGUAGES if code
]

# The recognition language. It has its own list, the third one, and not because the
# languages differ but because the meaning of the empty value differs. Here it is not
# "the model will decide for each reply" (where a switch in the middle of a phrase is
# what we want) but "recognition will guess the language from the sound". On slang and
# rare terms that guess produces exactly the slip into another language that the field
# was introduced for. The value goes to `realtime_stt.language`, not to `stt.language`:
# the latter has the same meaning but belongs to the ancestor field, which no engine
# reads now (see `perception/stt/realtime_openai.py`).
RECOGNITION_LANGUAGES = [("", "detect from audio (slower and less accurate)")] + [
    (code, label) for code, label, _rule in assist_prompt.LANGUAGES if code
]

# What kind of conversation. The list is short and ends with "other": it is impossible to
# list all kinds of conversations, and there is no need, since the list exists only so
# that the model understands the setting.

# The five steps of ReplyMode in order, from the noisiest to complete silence. The order
# in the list matches the order in the hint next to the field, not the declaration order
# in the enum, so that the person sees the ladder of trade-offs when opening the
# dropdown. One list serves both speech sources (microphone and system sound): both
# dropdowns offer the same steps.
REPLY_MODE_OPTIONS = [
    (ReplyMode.OFF.value, "don't answer"),
    (ReplyMode.ALL.value, "everything"),
    (ReplyMode.BY_FORM.value, "by phrasing"),
    (ReplyMode.WAKE_WORD.value, "by name"),
    (ReplyMode.SEMANTIC.value, "by meaning"),
]


def sections(tts_settings: TTSSettings) -> list[Section]:
    """Sections of two kinds. Everything else is either a duplicate or a developer concern.

    The selection followed a single criterion: without what the helper behaves differently
    from what the person needs, and what it cannot find out by itself.

    The kinds are in `Section.scope`. "chat" is what each chat has of its own (context,
    materials, language, when to answer): an edit goes to the open chat, not to the shared
    file (`argus/chats/`). "app" is shared by all chats: hotkeys, sound, voice-over, the
    app language. The home page shows only the second kind.

    What was removed and why:

    * "who you are" and "what to know about you" were two boxes for one text;
    * "what not to do" and "answer language" are already stated in the core prompt: "do not
      make up facts" and "answer in the language of the conversation";
    * "what kind of conversation this is" as a list is the same as the first line of the
      context, only as a dropdown;
    * answer length, the word threshold, silence on politeness, the pause, the cap per
      session, and the reaction to the screen all have reasonable defaults, and a person
      does not know what to set there until they see a problem;
    * provider, model, context length, and port are developer matters and live in
      `argus.toml`;
    * "show the screen image" is the same "Screen" checkbox in the readout;
    * "answer yourself" and "answer format" are knobs turned in the middle of a
      conversation, not set up before it. As a form of fields they looked like a setting.
      They are not on the readout yet either: `desk.py` was deleted from the channel, and
      until it returns both values are edited by hand in `argus.local.json`. They still
      apply to the live pipeline: `apply_assist` fires on reset;
    * "account": sign-in and password stood here as a separate section, duplicating the
      panel opened by the person button in the dashboard header. The form is one
      (`account.js`), but two places to sign in to the same thing make people wander: where
      to go if you signed in through settings once and through the panel another time.
      One door is left, the panel; there is nothing more about the account here.
    """
    return [
        # One window, and everything in it: who you are and what the conversation is. It used to
        # be two fields, and that turned out to be a division people do not make. They do not
        # sort themselves onto shelves; they paste text.
        Section(
            "about",
            "Conversation context",
            lead="Sent in full with every question — the longer it is, the more it costs. Write it "
                 "as it comes, this isn't a form.",
            scope="chat",
            settings=[
                # First line: the name of the chat as it appears in the list on the home page. This is a
                # property of the chat itself, not a config field. On the page it is visible only while
                # the chat is open (`AdminPanel.schema`).
                Setting("chat.name", "Session name", kind="line",
                        placeholder="It will be made up for you — or write your own",
                        hint="You can leave this empty: once the conversation description appears, "
                             "the name is made from it. A name you write here is never changed by "
                             "the app."),
                # No label: the section has the same name, and the same word a second time under the
                # heading is noise.
                Setting("channel.assist.profile.summary", "",
                        kind="text", rows=12,
                        placeholder="A short bio: who you are, what you've worked on, what the "
                                    "conversation will be about, and what questions might come up"),
                # Its own row, not a paragraph in the text above. It is switched often and quickly, for a
                # specific conversation, rather than written once and forgotten like the self-description.
                Setting("channel.assist.prompting.tone", "Answer tone", kind="choice",
                        options=assist_prompt.TONE_OPTIONS,
                        hint="Business — the usual tone for school and work. Casual — livelier, no "
                             "“Certainly!”, with swearing where it fits, like a real person."),
            ],
        ),
        # Right under the context, as a separate card: this is the same answer to the question
        # "what to answer from", only in files. It is a card of its own rather than a block in the
        # previous one, because a file list with the price in tokens takes half a screen, and
        # above the input field it would read as a header.
        Section(
            "materials",
            "Materials",
            lead="If you uncheck the box, the files stay here — the assistant just stops reading "
                 "them.",
            widget="materials",
            scope="chat",
            settings=[
                Setting("channel.assist.materials.enabled", "Use materials",
                        kind="bool"),
            ],
        ),
        Section(
            "language",
            "Language",
            lead="“Same as conversation” — the language isn't set in advance: the assistant picks "
                 "it on its own, for every answer.",
            scope="chat",
            settings=[
                Setting("channel.assist.profile.language", "Answer language", kind="choice",
                        options=assist_prompt.LANGUAGE_OPTIONS),
                Setting("realtime_stt.language", "Recognition language", kind="choice",
                        options=RECOGNITION_LANGUAGES,
                        hint="What the microphone hears, not the answer language. "
                             "With auto-detect, slang and rare words sometimes throw it "
                             "off to another language — pick the language explicitly to avoid that."),
                # We do not tell the person which model listens. The price of smart recognition is in
                # `server/pricing.py`.
                Setting("realtime_stt.smart", "Smart recognition", kind="bool",
                        hint="Handles strong accents and fast or unclear speech better. The text "
                             "may appear a little later, and an hour of recording costs more. Best "
                             "turned on before you start recording: if you switch it "
                             "mid-conversation, recognition starts over and forgets the last few "
                             "lines."),
            ],
        ),
        # "When to answer" used to be under "Audio", next to the microphone choice. But the
        # microphone is about hardware, and this is about the conversation: at a lecture you
        # answer by name, on a call to everything, and every chat has its own setting
        # (`argus/chats/`). Combining them on one card would mix what is common to all chats with
        # what each chat has of its own.
        Section(
            "answering",
            "When to answer",
            lead="Which lines make the assistant wake up on its own. In any mode you can still ask "
                 "it with the button or a hotkey.",
            scope="chat",
            settings=[
                # What physically reaches the microphone and the speakers is decided once, for a specific
                # conversation, and is not tuned in the middle of it. This pair does not go into "Hotkeys":
                # a separate check there runs every value of the section through the parser of
                # key combinations.
                #
                # There is no separate "answer automatically" anymore: that was `system_mode`, just
                # without its own name. The only reason to switch it off in the old form was exactly
                # "answer only on a button press", that is, "do not answer the other party". The step
                # "don't answer" here is exactly the same thing, but explicit and next to
                # the other steps rather than a separate checkbox above them.
                Setting("channel.assist.answering.mic_mode",
                        "Reply to microphone",
                        kind="choice",
                        options=REPLY_MODE_OPTIONS,
                        hint="For in-person meetings with one microphone for two people — otherwise "
                             "their questions never reach the assistant. “By phrasing” answers any "
                             "grammatical question, even a short one like “can you hear me?”; “by "
                             "name” and “by meaning” react only to a real address to the bot, and "
                             "cost more: “by meaning” makes an extra request to check."),
                Setting("channel.assist.answering.system_mode",
                        "Reply to system audio",
                        kind="choice",
                        options=REPLY_MODE_OPTIONS,
                        hint="The same, but for system audio — what the other person is physically "
                             "saying. “Don't answer” — only via the “Ask” button or hotkey. “By "
                             "phrasing” is the default and answers any grammatical question, even "
                             "“can you hear me?”; “by name” is for a group call or a lecture, where "
                             "it should answer only when addressed directly, not every question "
                             "from someone else."),
                Setting("channel.assist.answering.wake_word",
                        "Bot name",
                        kind="line",
                        placeholder="e.g., Argus",
                        hint="The bot uses this word to know you're talking to it — shared by the "
                             "microphone and system audio.",
                        depends_on=(
                            ("channel.assist.answering.mic_mode", ReplyMode.WAKE_WORD.value),
                            ("channel.assist.answering.system_mode", ReplyMode.WAKE_WORD.value),
                        )),
                Setting("channel.assist.answering.think_longer",
                        "Think longer",
                        kind="bool",
                        hint="Slower, but more thought through."),
            ],
        ),
        # The "Window now" toggle lives neither here nor in "Hotkeys". It
        # has its own static block on the page (`admin.html`, outside `A.schema.sections`). It is
        # not a key combination and not a setting for the future but the state of the window right
        # now, and an extra section in the list on the left would not reflect it correctly. The
        # test `test_page_shows_only_general_settings` keeps the list of sections small on
        # purpose.
        Section(
            "hotkeys",
            "Hotkeys",
            # This is not obvious from the list of combinations itself: they are global
            # (`RegisterHotKey`, see `hotkeys.py`) and therefore work from any window on purpose, not
            # by accident. In the middle of a conversation you cannot switch to this window just to
            # press a button.
            lead="They work from any window, not just this one — otherwise you couldn't press them "
                 "in the middle of a conversation.",
            settings=[
                Setting("channel.assist.hotkeys.ask", "Ask", kind="hotkey"),
                Setting("channel.assist.hotkeys.hide_main", "Open/close main window", kind="hotkey"),
                Setting("channel.assist.hotkeys.hide_panel", "Open/close panel", kind="hotkey"),
                Setting("channel.assist.hotkeys.ask_screen", "Ask about the screen", kind="hotkey"),
                Setting("channel.assist.hotkeys.recognition", "Recognition on/off", kind="hotkey"),
            ],
        ),
        Section(
            "audio",
            "Audio",
            # The only section whose defaults cannot be trusted blindly; see the docstring of the
            # whole module at the top of the file.
            lead=f"Check before a conversation: {'macOS' if sys.platform == 'darwin' else 'Windows'} "
                 "sometimes swaps the microphone on its own, and then you just get silence, with no error.",
            widget="audio-check",
        ),
        # A test feature, not a product knob: a voice cloned or taken from ElevenLabs reads the
        # reply aloud. `argus sync-voices` / `argus import-voice` fill the list of characters;
        # here they are chosen: searched, filtered by language, and marked as favorites.
        Section(
            "tts",
            "Voice replies (test)",
            lead="Spoken answers — paid, by the number of characters. Off by default. A new voice "
                 "will show up here after you restart the app.",
            widget="voice-picker",
            settings=[
                # Hidden (see `Setting.hidden` below): the widget draws it itself, with its own switch
                # above the search, as the first line of the section, above the list itself. The order
                # ("first the switch, then the choice of a character") matters to the person and cannot
                # be expressed through the frame: `section()` in `admin.js` always puts the `widget`
                # BEFORE all of `.fields`, regardless of the order of `settings` here. So since the order
                # decides everything, the widget itself decides it, not the frame. Checked in the code,
                # not only by name: `_speak` in `tts/reply.py` quietly exits if no character is selected.
                # There is no error on the page and no sound, so there is nothing to guess from.
                Setting("channel.tts.enabled", "Read answers aloud", kind="bool", hidden=True),
                # Hidden for the same reason: the character is chosen with the cards of the list below,
                # in the same widget, not with a separate `<select>`. A second, ordinary list with the
                # same voices would read as a second place to do the same thing. The row stays registered
                # for the sake of `apply()`/`A.values`, like `favorite` below.
                Setting("channel.tts.active_character", "Voice",
                        kind="choice",
                        options=[
                            ("", "— no voice selected —"),
                            *_character_options(tts_settings),
                        ],
                        hidden=True),
                # Hidden: they get no row of their own (see `Setting.hidden`), and the "voice-picker"
                # widget draws them as a star on the voice card. They are declared here, and not only in
                # `VoiceProfile`, so that `apply()` accepts the edit at all. Otherwise a path unknown to
                # it would be quietly filtered out, and "favorite" would never be saved, without a single
                # error on the page.
                *(
                    Setting(f"channel.tts.characters.{slug}.favorite",
                            f"★ {p.label or slug}", kind="bool", hidden=True)
                    for slug, p in tts_settings.characters.items()
                ),
            ],
        ),
        # The language of the app itself is common to all chats, so it is not under "Language".
        # That section holds the reply and recognition languages, and each chat has
        # its own.
        Section(
            "interface",
            "App",
            settings=[
                # Next to "Invisible in recordings" (`admin.js` draws that one itself):
                # the taskbar is part of the shared screen. Windows only. On macOS the window has neither
                # a taskbar button nor an Alt+Tab entry that could be removed, and the switch would
                # promise something that does not exist. There the setting is declared but has no row
                # (`hidden`, like the voice settings).
                Setting("ui.main_taskbar_button", "Window button on the taskbar", kind="bool",
                        hidden=sys.platform == "darwin",
                        hint="The taskbar is visible when you share your screen, and the “Argus” "
                             "button on it shows that the app is open. Turn this off and there is "
                             "no button on the taskbar or in Alt+Tab. You can bring the window back "
                             "with the “Open/close main window” hotkey in the “Hotkeys” section, or "
                             "by starting Argus again; “Minimize” then hides the window."),
                Setting("channel.assist.interface.language", "App language",
                        kind="choice", options=APP_LANGUAGES),
            ],
        ),
    ]


def _character_options(tts_settings: TTSSettings) -> list[tuple[str, str]]:
    """Characters for the dropdown: favorites first, then by name.

    The label carries the language and a star right in the text. The list can be several
    dozen entries longer than the original three, and without this hint, choosing with a
    bare `<select>` (no JS, the fallback in case the widget did not mount) would turn into
    guessing from a bare name.
    """
    def label(key: str, p: VoiceProfile) -> str:
        star = "★ " if p.favorite else ""
        lang = f" · {p.language}" if p.language else ""
        return f"{star}{p.label or key}{lang}"

    ordered = sorted(
        tts_settings.characters.items(),
        key=lambda kv: (not kv[1].favorite, (kv[1].label or kv[0]).lower()),
    )
    return [(key, label(key, p)) for key, p in ordered]


# --------------------------------------------------------------------------
# What to do with an edit
# --------------------------------------------------------------------------


def apply_assist(app: Argus, cfg: Config, changed: set[str]) -> None:
    """Carry the channel's edits to the running pipeline.

    The self-description and the conversation context rebuild the system prompt. It is
    derived from them, not a separate setting, and keeping a copy of it would be a way to
    drift apart one day. The filter is rebuilt with its counters preserved.
    """
    if not any(p.startswith(f"channel.{KEY}.") for p in changed):
        return
    settings = read(cfg)
    if any(p.startswith(f"channel.{KEY}.answering.") for p in changed):
        assist_gate.install(app, settings)
    # With `app`: the rebuilt prompt is a new prefix, and the cache has to be written again
    # for it. The edit is made before the conversation, where there is a second to spare; the
    # first question must not pay for it.
    assist_prompt.install(cfg, app)


def apply_tts(app: Argus, cfg: Config, changed: set[str]) -> None:
    """Carry a voice-over edit to the running app.

    `channel.tts.*` is a separate, free-form key of the `channel` section, not part of
    `AssistSettings`. `TTSSettings` validates it through `extra_channel_models` in
    `build_panel()`, the same way as `assist`.
    """
    if not any(p.startswith(f"channel.{TTS_KEY}.") for p in changed):
        return
    tts_reply.install(app, tts_read(cfg))


def apply_hotkeys(app: Argus, cfg: Config, changed: set[str]) -> None:
    """Rebind the key combinations on the running app.

    The registration belongs to the thread that made it, so only an order is placed here:
    the thread itself will drop the old combinations and set the new ones on its next
    round. Without this, an edit would land in the file and wait for a restart. But people
    check it right away by pressing the keys, and "did not work" would be
    indistinguishable from "the combination is taken".

    There are no hotkeys in the browser at all: the window thread holds them.
    """
    if not any(p.startswith(f"channel.{KEY}.hotkeys.") for p in changed):
        return

    from argus.desktop import hotkeys

    keys = hotkeys()
    if keys is None:
        return
    keys.bind(read(cfg).hotkeys.model_dump())


def apply_window(app: Argus, cfg: Config, changed: set[str]) -> None:
    """The main window's taskbar button, immediately, without a restart.

    The value is already in the live config; here we only place an order with the window
    (it switches in its own thread). In a browser, and without a window, it does nothing.
    """
    if "ui.main_taskbar_button" not in changed:
        return

    from argus.desktop import set_main_taskbar_button

    set_main_taskbar_button(bool(cfg.ui.main_taskbar_button))


def build_panel(app: Argus) -> AdminPanel:
    """Build the admin page and immediately bring the pipeline in line with the settings.

    Installing at startup is as mandatory as applying an edit. The prompt and the filter are
    assembled from the settings rather than stored, and without this step the first session
    after a restart would run on the core defaults.
    """
    settings = read(app.cfg)
    assist_prompt.install(app.cfg, app)
    assist_gate.install(app, settings)
    tts_settings = tts_read(app.cfg)
    tts_reply.install(app, tts_settings)

    panel = AdminPanel(
        app,
        title="Argus · assistant",
        subtitle="hears the conversation, sees the screen and prompts you — in interviews, "
                 "exams, calls",
        channel=KEY,
        accent=ACCENT,
        sections=sections(tts_settings),
        page=PAGE,
        channel_model=AssistSettings,
        extra_channel_models={TTS_KEY: TTSSettings},
        appliers=[apply_assist, apply_hotkeys, apply_tts, apply_window],
        routes=[_routes(app), _account_routes(app), tts_reply.routes(app)],
    )

    return panel


# --------------------------------------------------------------------------
# Own routes
# --------------------------------------------------------------------------


def account_of(app: Argus):
    """The link to the account server, one per app.

    It is created lazily and remembered on `app`. The refresh token and the short-lived
    access token live inside, and there is no need to create them again on every page
    request.

    An empty server address means there are no accounts at all: your own build, your own
    keys, no balance. Then there is no section in the settings either.
    """
    if not app.cfg.account.server:
        return None
    existing = getattr(app, "_account", None)
    if existing is None:
        from argus.account.client import Account

        existing = Account(app.cfg.account.server, app.cfg.root)
        app._account = existing
    return existing


# The payment checkouts a person may be sent to for paying, other than the account server
# itself.
CASHIER_HOSTS = ("liqpay.ua", "www.liqpay.ua")


def pay_link(url: object, server: str) -> tuple[str, bool]:
    """The payment address from the server: what to show the page, and whether we may open it
    ourselves.

    Any http(s) address without credentials may be shown as text; the person decides
    whether to open it. We open it ourselves only for https to the account server or to a
    payment host from `CASHIER_HOSTS`, and for http to the account server itself, exactly as
    it is written in `[account] server` (while it has no TLS). Everything else (a path to a
    program, a network path, a foreign scheme) is neither shown nor opened.
    """
    if not isinstance(url, str):
        return "", False
    url = url.strip()
    if not url or "\\" in url or any(c.isspace() or ord(c) < 32 for c in url):
        return "", False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        own = (urlsplit(server).hostname or "").lower()
    except ValueError:
        return "", False
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        return "", False
    safe = host == own if parts.scheme == "http" else host in (own, *CASHIER_HOSTS)
    return url, bool(own) and safe


def _account_routes(app: Argus):
    """Sign-in and balance, from the settings page.

    The page decides nothing by itself: each handler is one call to the server and a
    display of its reply. A refusal arrives as text and is shown as text, without being
    translated into "something went wrong". Running out of minutes, a wrong password, and
    a taken email are three different next steps.
    """

    def add(web) -> None:
        from argus.account.client import AccountError

        def need():
            acc = account_of(app)
            if acc is None:
                raise HTTPException(404, "this build has no accounts")
            return acc

        # Every `Account` call is an ordinary synchronous request to the server (up to ten
        # seconds, and as much again when the sign-in is refreshed). Made directly in an `async`
        # handler, it would hold the whole event loop: windows, sound, and replies would stand
        # still while the account server thinks. And the page asks it on every load and every four
        # seconds while it waits for a payment. So everything here goes through
        # `asyncio.to_thread`.
        @web.get("/admin/account")
        async def account_state() -> dict:
            from argus.account import legal

            state = await asyncio.to_thread(need().state)
            # The terms version travels together with the state. The "accept the terms" banner uses
            # it to decide whether to show itself, and a separate request for one string would be an
            # extra round trip.
            return {**state, "legal": {"terms": legal.TERMS_VERSION}}

        def _mail_lang() -> str:
            """The app language, passed to the account server: emails (welcome, payment, balance,
            recovery) are sent in it.
            """
            from argus.ui import i18n

            return i18n.resolve(i18n.setting_of(app))

        @web.post("/admin/account/register")
        async def account_register(payload: dict) -> dict:
            acc = need()
            try:
                await asyncio.to_thread(acc.register, str(payload.get("email", "")),
                                        str(payload.get("password", "")), lang=_mail_lang())
            except AccountError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, **await asyncio.to_thread(acc.state)}

        @web.post("/admin/account/terms")
        async def account_terms() -> dict:
            """"I accept" is for an account created before the terms existed, or for a new
            version.

            The page does not send a version. The one accepted is the one in this app
            (`account.legal.TERMS_VERSION`), that is, the one the page has just shown.
            """
            acc = need()
            try:
                await asyncio.to_thread(acc.accept_terms)
            except AccountError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, **await asyncio.to_thread(acc.state)}

        @web.post("/admin/account/export")
        async def account_export() -> dict:
            """Save a copy of the account data to the Downloads folder and say where.

            As a file on disk, not as a download from the page: the app window is an embedded
            browser, and "save as" from it is not always available.
            """
            import json
            import time
            from pathlib import Path

            acc = need()
            try:
                data = await asyncio.to_thread(acc.export_data)
            except AccountError as e:
                return {"ok": False, "error": str(e)}
            folder = Path.home() / "Downloads"
            if not folder.is_dir():
                folder = app.cfg.root
            path = folder / f"argus-my-data-{time.strftime('%Y-%m-%d')}.json"
            try:
                await asyncio.to_thread(
                    path.write_text, json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except OSError as e:
                log.warning("data copy was not written: %s", e)
                return {"ok": False, "error": "couldn't save the file"}
            return {"ok": True, "path": str(path)}

        @web.post("/admin/account/delete")
        async def account_delete(payload: dict) -> dict:
            """Delete the account. The password is the one the person has just entered."""
            acc = need()
            try:
                await asyncio.to_thread(acc.delete_account, str(payload.get("password", "")))
            except AccountError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True}

        @web.post("/admin/account/password")
        async def account_password(payload: dict) -> dict:
            """Change the password: old and new, both just entered by the person."""
            acc = need()
            try:
                await asyncio.to_thread(acc.change_password, str(payload.get("old", "")),
                                        str(payload.get("new", "")))
            except AccountError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True}

        @web.post("/admin/account/sign-in")
        async def account_sign_in(payload: dict) -> dict:
            acc = need()
            try:
                await asyncio.to_thread(acc.sign_in, str(payload.get("email", "")),
                                        str(payload.get("password", "")), lang=_mail_lang())
            except AccountError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, **await asyncio.to_thread(acc.state)}

        @web.post("/admin/account/sign-out")
        async def account_sign_out() -> dict:
            await asyncio.to_thread(need().sign_out)
            return {"ok": True}

        @web.post("/admin/account/reset")
        async def account_reset(payload: dict) -> dict:
            """A request to recover the password. Always "ok".

            The account server itself answers the same way whether or not an account with this email
            exists. Here it is the same: the page must not get a chance to tell these cases apart
            by the reply.
            """
            acc = need()
            try:
                await asyncio.to_thread(acc.request_reset, str(payload.get("email", "")), lang=_mail_lang())
            except AccountError as e:
                log.warning("recovery request did not go through: %s", e)
            return {"ok": True}

        @web.get("/admin/account/pricing")
        async def account_pricing() -> dict:
            acc = need()
            try:
                return {"ok": True, **await asyncio.to_thread(acc.pricing)}
            except AccountError as e:
                return {"ok": False, "error": str(e)}

        @web.get("/admin/account/history")
        async def account_history() -> dict:
            acc = need()
            try:
                return {"ok": True, **await asyncio.to_thread(acc.history)}
            except AccountError as e:
                return {"ok": False, "error": str(e)}

        @web.get("/admin/account/usage")
        async def account_usage(tz: int = 0) -> dict:
            acc = need()
            try:
                return {"ok": True, **await asyncio.to_thread(acc.usage, tz)}
            except AccountError as e:
                return {"ok": False, "error": str(e)}

        @web.post("/admin/account/checkout")
        async def account_checkout(payload: dict) -> dict:
            """Create a top-up order and open the payment page in the browser.

            Specifically in the browser, not in the app window: our window is frameless, on top of
            everything, and invisible to screen capture. A payment page does not belong there, and
            the person would not see the bank confirmation in it.
            """
            import webbrowser

            from argus.ui import i18n

            acc = need()
            dollars = payload.get("dollars", 0)
            # An integer; a string with a number is fine too. Anything else is a refusal in human
            # words, not the text of a Python exception.
            not_a_sum = {"ok": False, "error": "the amount must be a whole number of dollars"}
            if isinstance(dollars, bool) or not isinstance(dollars, int | str):
                return not_a_sum
            try:
                dollars = int(dollars)
            except ValueError:
                return not_a_sum
            try:
                # The app's language goes to the checkout: it will show the payment page in that language.
                order = await asyncio.to_thread(
                    acc.checkout, dollars, i18n.resolve(i18n.setting_of(app)))
            except AccountError as e:
                return {"ok": False, "error": str(e)}
            except (ValueError, TypeError) as e:
                # The server reply could not be parsed (a stub page instead of a reply, a cut-off in the
                # middle). The person gets a generic phrase, not the exception text; the details go to
                # the log.
                log.warning("order response could not be parsed: %s: %s", type(e).__name__, e)
                return {"ok": False, "error": friendly.explain(e, trust_text=False)}
            if not isinstance(order, dict):
                return {"ok": False, "error": friendly.OTHER}

            # The address comes from the server over plain HTTP, and `webbrowser` opens it, which on
            # Windows means `os.startfile`: a path to a program, a network path, and any scheme would
            # work as a command. We open only what `pay_link` recognized as ours.
            url, safe = pay_link(order.get("pay_url", ""), app.cfg.account.server)
            opened = False
            if safe:
                # We do not fail if there is nothing to open it with: the address goes to the page anyway,
                # and the person will open it themselves.
                try:
                    opened = bool(await asyncio.to_thread(webbrowser.open, url))
                except Exception:  # noqa: BLE001 — there may be no browser at all
                    log.warning("could not open the browser at %s", url)
            elif order.get("pay_url"):
                log.warning("payment URL not opened: wrong server or not http(s)")
            # Our own fields come after the server reply: it must not override `ok` and `opened`.
            return {**order, "ok": True, "opened": opened, "pay_url": url}

    return add


def _routes(app: Argus):
    """The assembled prompt as a whole. The page no longer has a block for it.

    The preview with the price in tokens was removed from the page: above two fields it
    took more room than the fields themselves. The route stays as a debugging aid. It is
    the only way to see what really goes to the model, and the cost of the self-description,
    which goes in full into every request.
    """

    def add(web) -> None:
        @web.get("/admin/assist/prompt")
        async def prompt_preview() -> dict:
            """What goes to the model on top of the timeline, in full and with the price in tokens.

            A self-description easily grows to several pages, and it is in every request. Seeing the
            price next to the text is the only way not to discover it from a monthly bill.
            """
            from argus.context.render import estimate_tokens

            # We take the **current** prompt, not one rebuilt from the settings. A rebuilt one is
            # almost the same, and all the trouble is in that "almost": the materials do not get into
            # it, and the debug route would show a prompt without the lecture that does go to the
            # model.
            #
            # There are two variants, with a screen frame attached and without it, and the choice
            # between them is made anew for every question, not in advance. The debug route must
            # answer both "what goes out" cases, not lie with half the truth about whichever one
            # would happen to be chosen.
            text = app.cfg.brain.system_prompt
            text_with_image = app.cfg.brain.system_prompt_with_image
            return {
                "text": text, "chars": len(text), "tokens": estimate_tokens(text),
                "text_with_image": text_with_image,
                "chars_with_image": len(text_with_image),
                "tokens_with_image": estimate_tokens(text_with_image),
            }

        @web.get("/admin/assist/materials")
        async def materials_list() -> dict:
            return await _materials(app)

        @web.post("/admin/assist/materials")
        async def materials_add(request: Request) -> dict:
            """Accept a file as the request body.

            As the body, not as a form: a form would pull in `python-multipart` for a single field.
            The name arrives in the address, and only the file name is taken from it. Otherwise
            "../../argus.toml" would be written exactly where it asked.

            A foreign page is kept out by the guard (`ui/guard.py`). Without it, any website would
            plant its own text into the materials, and the materials go into every question.
            """
            name = Path(request.query_params.get("name", "")).name
            suffix = Path(name).suffix.lower()
            if not name or name.startswith("."):
                return {"ok": False, "error": "a file name is required"}
            if (INVALID_NAME.search(name) or name != name.rstrip(" .")
                    or name.split(".")[0].strip().upper() in RESERVED_NAMES):
                return {"ok": False,
                        "error": "this file name won't do — rename it and upload again"}
            if suffix not in materials.SUFFIXES:
                return {"ok": False,
                        "error": f"“{suffix or "no extension"}” can't be read; "
                                 f"these work: {', '.join(materials.SUFFIXES)}"}
            too_big = {"ok": False, "error": f"larger than {MAX_UPLOAD // 1024 // 1024} MB — I won't take it"}

            # The size is known before the first byte. We do not read a body that we would refuse
            # anyway.
            declared = request.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_UPLOAD:
                return too_big

            # The body goes in chunks into a temporary file next to the target, with a hard cap.
            # Reading it whole into memory (`request.body()`) would let a gigabyte request eat the
            # memory before any size check. The file gets its real name by a single rename, and the
            # materials parser never sees a half-written file: the temporary one has the `.part`
            # extension, which the folder reader ignores.
            folder = materials.directory(app.cfg)
            part = folder / f".upload-{secrets.token_hex(6)}.part"
            size = 0
            try:
                folder.mkdir(parents=True, exist_ok=True)
                with part.open("wb") as out:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_UPLOAD:
                            return too_big
                        out.write(chunk)
                if not size:
                    return {"ok": False, "error": "the file is empty"}
                os.replace(part, folder / name)
            except OSError as e:
                log.warning("material was not saved: %s", e)
                return {"ok": False, "error": friendly.plain_or(e, "couldn't save the file")}
            finally:
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    log.warning("temporary upload file was not removed: %s", part.name)
            log.info("material added: %s (size in KB: %d)", name, size // 1024)
            return await _materials(app, rebuild=True)

        @web.post("/admin/assist/materials/delete")
        async def materials_delete(payload: dict) -> dict:
            name = Path(str(payload.get("name", ""))).name
            path = materials.directory(app.cfg) / name
            if name and path.is_file():
                path.unlink()
                log.info("material removed: %s", name)
            return await _materials(app, rebuild=True)

        @web.post("/admin/hotkeys/capture")
        async def hotkeys_capture(payload: dict | None = None) -> dict:
            """Release the key combinations while one is being assigned, and give them back afterward.

            Otherwise a combination that we ourselves hold cannot be assigned at all: the system
            delivers the keypress to whoever registered it.

            `held` tells the page the truth about whether there was anything to release. A browser
            tab has no hotkeys at all, and keypresses can be captured there right away.
            """
            from argus.desktop import hotkeys

            keys = hotkeys()
            if keys is None:
                return {"held": False}
            # In a thread: `suspend()` waits for the combinations thread, and if that thread is dead,
            # for a whole second, and the event loop would stand still for all that time.
            if (payload or {}).get("on"):
                await asyncio.to_thread(keys.suspend)
            else:
                await asyncio.to_thread(keys.resume)
            return {"held": True}

    return add


async def _materials(app: Argus, rebuild: bool = False) -> dict:
    """What is in the folder and what it costs in every request.

    The price is shown next to the list for a reason. Materials go to the model whole and
    with every question. A person who dropped in a folder with a semester of course
    material would find out about it from a monthly bill. Here they see it before the exam.

    `rebuild` rebuilds the prompt and warms the cache: the number of files changed, so both
    are out of date.
    """
    from argus.context.render import estimate_tokens

    settings = read(app.cfg)
    m = settings.materials
    # Parsing the files means reading, re-encoding photos, and rendering the pages of scanned
    # PDFs: seconds, and tens of seconds for a large scan. Directly in the handler on the
    # event loop they would hold all windows, sound, and replies, so the parsing goes to a
    # thread.
    #
    # Under a lock, one request at a time. Before, everything ran on one loop and one request
    # after another. Now the list, the upload, and the deletion could parse the folder at the
    # same time, and the page-cache cleanup of one (`_sweep_cache`) would erase the pages that
    # another had just written.
    lock = getattr(app, "_materials_lock", None)
    if lock is None:
        lock = app._materials_lock = asyncio.Lock()
    async with lock:
        loaded = await asyncio.to_thread(materials.load, app.cfg, m.max_chars, m.max_pages)
        if rebuild:
            # The prompt is rebuilt here, on the loop, not in the thread: `install` wakes up cache
            # warming through `warm_soon()`, and in a thread without a loop it silently does nothing.
            # What was parsed above is remembered, so the second parse inside `install` costs almost
            # nothing.
            assist_prompt.install(app.cfg, app)

    return {
        "ok": True,
        "items": [
            {"name": i.name, "kb": i.bytes // 1024, "chars": i.chars,
             "pages": i.pages, "error": i.error}
            for i in loaded.items
        ],
        "pages": len(loaded.images),
        "dropped": loaded.dropped,
        "dropped_pages": loaded.dropped_pages,
        # A page as an image is about fifteen hundred tokens. Text is counted by the same measure
        # as the timeline. The number is approximate, and that is honest: only the model knows the
        # exact one, and the order of magnitude is visible anyway.
        "tokens": estimate_tokens(loaded.text) + len(loaded.images) * PAGE_TOKENS,
        "enabled": m.enabled,
    }
