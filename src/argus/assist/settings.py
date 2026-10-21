"""Settings of the `assist` channel. They live in `channel.assist`; the core does not parse
them.

The order of the sections here is not accidental; it follows the order of importance from
the branch plan. The profile comes first: the model answers "Tell me about the GIL" even
without it, but "why did you leave your last job" gets a useless answer without a résumé.
This is the main thing that sets the channel apart from the bare core, and that is why it
is the first section of the admin page.

Next come stealth and silence. An overlay that is visible in a screen share is worse than
none; so is a hint that floods the screen with text exactly when you should be listening.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from argus.brain.gate import ReplyMode


class Profile(BaseModel):
    """Who the user is. It goes into the system prompt on every request.

    Not "additional context" but half the point of the channel. Without it the model answers
    like a textbook: correctly and off target. "Tell me about your experience with queues"
    with no facts about the person turns into a retelling of the RabbitMQ documentation, when
    what was needed was an answer about what they did themselves.

    There are more fields than the admin page shows, and that is not a legacy leftover. The
    admin page is reduced to four general ones (who you are, what to know about you, what not
    to do, language), and the rest stay: they are edited from the control panel and by those
    who need to lay out their story about themselves on shelves. Removing them would erase
    what is already saved.
    """

    name: str = ""
    role: str = ""
    years: int = Field(default=0, ge=0, le=60)

    # Stack in one line: languages, databases, tools. The model uses it to choose what level to
    # answer at and in what words.
    stack: str = ""

    # Context in free form: not a résumé, there is no formal résumé here. Long is fine: it goes
    # into the system prompt, not the timeline, and when working through the subscription it is
    # cached between requests.
    summary: str = ""

    projects: str = ""

    # Prepared answers to questions about yourself. The only class of questions where the model
    # physically cannot get by on subject knowledge: why you left, what you are looking for,
    # where you see yourself. Without the user's answers there will be filler here.
    story: str = ""

    # What not to do. Insurance against a beautiful lie that would have to be defended out
    # loud: the model invents a project in a second, while the person has to answer for it for
    # forty minutes. It goes into the prompt as a separate section, because a prohibition
    # dissolved in the description of experience is read by the model as a wish.
    stop_topics: str = ""

    # Answer language. Empty means as in the conversation; see LANGUAGES in
    # `assist/prompt.py`. This used to hold "ru", but nobody read the field. Keeping it when
    # switching the field on would silently force the model to answer in Russian where
    # yesterday it followed the conversation.
    language: str = ""


class Vacancy(BaseModel):
    """About this conversation: the only thing that changes from one time to the next.

    The class name is left over from the first idea, when the assistant was only about
    interviews. It lies: by meaning this is not a vacancy but a conversation, what kind it is
    and in what words to answer. The same question in an interview, in an exam, and on a call
    needs a different answer, and the model can learn that only from here.

    Renaming the class is cheap, but the settings key is not: saved files point at these
    paths, and a drifted name means silently lost data for anyone who has already written
    something into them.
    """

    company: str = ""
    position: str = ""

    # Context of the conversation: a job description, an exam topic, a call agenda. The model
    # uses it to understand which words to treat as its own: "sharding" in an interview for a
    # high-load job and in a databases exam calls for different answers.
    description: str = ""

    # Kind of conversation. Empty means not specified: all kinds cannot be listed, and an
    # invented default would confuse the model more often than it helped.
    stage: str = ""


class Answering(BaseModel):
    """When to answer and how briefly.

    A reaction to every utterance is the main trouble of the channel: the overlay floods the
    screen with text exactly when you need to listen to the other party. The core's filter
    cuts by the question mark and the first word; at that level «Слышно меня?» (Can you hear
    me?) is already a question, and by default (`BY_FORM`) it gets an answer. The "chatter"
    and "few words" filters below are a free pre-filter only before the paid model call in
    `ReplyMode.SEMANTIC`, not a general rule of the channel.
    """

    ignore_small_talk: bool = True

    # An utterance shorter than this is almost always a nod of agreement. We count in words,
    # not characters: «Что такое GIL?» (What is the GIL?) is never shorter than twelve
    # characters, while «Ага, понял, спасибо» (Yeah, got it, thanks) is longer, and would have
    # passed by characters.
    min_words: int = Field(default=4, ge=1, le=30)

    stop_phrases: list[str] = Field(
        default_factory=lambda: [
            "слышно меня", "меня слышно", "видно экран", "всё хорошо",
            "давайте начнём", "приятно познакомиться", "спасибо за ответ",
            "секунду", "минутку", "хорошо, понял", "да, понятно",
        ]
    )

    # Answer length. The overlay is read by eye without taking attention from the
    # conversation: an answer that cannot be read in three seconds will not be read at all.
    max_answer_chars: int = Field(default=600, ge=100, le=4000)

    style: str = "short and to the point"

    # Answer form. It switches in the middle of a call, at the move to coding, and so it is
    # not the same as `style`: with one motion the length, the tone, and what the answer
    # starts with all change. The conversational part needs a line you can read between
    # utterances; in CoderPad, code.
    format: str = "line"

    # Whether to answer utterances from the microphone and not only from system audio, and how
    # selectively. The default is `OFF`: your own voice is not a question, and in a call with
    # separate channels a microphone utterance is guaranteed to be someone else's anyway.
    # The other steps are for an in-person meeting with one microphone for two, where the
    # other party's voice physically lands in the same channel as your own, from "answer to
    # everything" to "only by name" and "only by meaning". The steps themselves and their
    # order are explained at `ReplyMode`. It lives here, not in the core's `[gate_brain]`, for
    # exactly the same reason as `format`: it is a knob about a specific conversation, not
    # about the engine.
    mic_mode: ReplyMode = ReplyMode.OFF

    # The same ladder, but for system audio, that is, for the other party. The default is
    # `BY_FORM`: it answers any grammatical question, however short or trivial («Слышно меня?»
    # (Can you hear me?) is a question too, and it is worth answering). The steps below and
    # above are already a deliberate choice: "by name" for a group conversation, where only a
    # direct address to the bot should be answered; "by meaning" when the form is not enough
    # and the model's judgment is needed; and "everything" when the filter itself gets in the
    # way (a lecture without a clear structure of questions).
    system_mode: ReplyMode = ReplyMode.BY_FORM

    # The address word for `ReplyMode.WAKE_WORD`, shared by `mic_mode` and `system_mode`: what
    # the bot is called in the conversation, not what it is called from the microphone
    # separately from what it is called from system audio. Empty by default: without a name the
    # "by name" mode simply never fires, and does not fail or secretly fall back to another
    # mode.
    wake_word: str = ""

    # "Think longer" from the control panel next to the input field. It used
    # to live only in the browser (`localStorage`); it seemed reasonable not to send to the
    # server what is clicked on every question. But the "ask" hotkey works differently: it is
    # pressed in `desktop.py`, a separate process with no access to the page and its
    # `localStorage`, and it always calls `/ask` with an empty body, whatever is set in the
    # browser. The person switched on "think longer" on the panel and never got it when asking
    # by hotkey rather than by clicking the "Ask" button. A real channel setting,
    # like the other three knobs of the panel, read from there and from here in the same way.
    think_longer: bool = False


class Overlay(BaseModel):
    """A hidden overlay is what the channel exists for in the first place.

    A dashboard on localhost will not do: it is visible in a screen share and needs
    alt-tabbing, and mouse movement is noticeable in an interview. The right answer is a
    window on top of everything with `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`:
    visible to you, invisible in the capture (Win10 2004+).

    **Nothing in this section is read right now** (neither `anchor`, nor `width`, nor
    `exclude_from_capture` below): the application windows do not depend on these fields. The
    real and only switch for capture protection is `ui.exclude_from_capture`
    (`core/config.py`, `argus.toml`). The section is kept because saved settings files and the
    page schema point at its path; editing these fields changes nothing.
    """

    enabled: bool = True

    # Where to snap the window. Top right by default: that is where Zoom puts the participant
    # tiles, so the eyes go there anyway.
    anchor: str = "top-right"

    width: int = Field(default=520, ge=240, le=1600)
    max_lines: int = Field(default=14, ge=3, le=60)
    font_size: int = Field(default=15, ge=9, le=32)
    opacity: float = Field(default=0.92, ge=0.2, le=1.0)

    # After how long to hide the answer. An old answer hanging on the screen distracts just as
    # much as a new one at the wrong time.
    hide_after_s: float = Field(default=25.0, ge=0.0, le=600.0)

    # Not read (see the class docstring): the real flag is `ui.exclude_from_capture`. Turn it
    # off only for debugging: without it the windows end up in the screen share, and that is
    # the worst thing that can happen in this channel.
    exclude_from_capture: bool = True


class Hotkeys(BaseModel):
    """Global hotkeys.

    You cannot alt-tab and you cannot move the mouse. So all control is from the keyboard,
    and the keys must work while the focus is in Zoom.

    Five, no more: ask, ask with a screen frame, turn recognition on/off, and separately
    open/close the main window and open/close the detached panel. These are not the same
    thing: the main window is the dashboard itself, the panel is the overlay on top of the
    call, and hiding one with a keypress without touching the other is what they were put on
    different shortcuts for in the first place.

    There used to be two more, "scroll" and "panic" (hide without asking about the state),
    and both were dropped: they are needed less often than the others, and on a keyboard
    under stress it is more important to keep fewer shortcuts in mind, not more.
    """

    ask: str = "ctrl+alt+a"

    # The main window is the dashboard itself (the same page as in the browser).
    hide_main: str = "ctrl+alt+w"

    # The detached panel is the overlay on top of the call. If there is none, the shortcut
    # opens one itself rather than staying silent.
    hide_panel: str = "ctrl+alt+h"

    # The same question as `ask`, but with a screen frame. There used to be a separate button
    # on the panel ("ask about the screen"); now it is the same thing from the keyboard,
    # without a second button in the interface.
    ask_screen: str = "ctrl+alt+s"

    # The common switch: the same as the "power" button on the panel, only you do not have to
    # reach for it by hand.
    recognition: str = "ctrl+alt+p"


class Prompting(BaseModel):
    """What the model receives on top of the profile."""

    # Empty means the core's general prompt is used. People write their own when the general
    # one is not enough.
    system_base: str = ""

    # Whether to put a screenshot into the prompt. In an interview, yes: the task statement and
    # the code in CoderPad live on the screen, not in the conversation.
    send_screen: bool = True

    # Business or informal. A string with two values, not a boolean field: adding a third mode
    # someday is easier than turning a boolean field into an enum after the fact, when
    # `true`/`false` is already saved somewhere. The values and the ready text for each are in
    # `TONES` (`assist/prompt.py`); here is only the choice of what to substitute.
    #
    # The tone is not written in the person's own words: the model hardly converts "answer
    # informally" into behavior, treating it abstractly and playing safe with politeness where
    # the opposite was asked for. The choice is only between two ready, tested texts.
    #
    # Business by default: the section works for interviews and for study, and swearing with
    # "so, listen" is out of place there. Informal is for conversations that are not about
    # work, where the same thing, on the contrary, livens up the answer.
    tone: str = "business"


class Materials(BaseModel):
    """Lectures and notes: what the model is required to answer from.

    The files themselves are in the `materials/` folder next to `argus.toml`; here is only
    what to do with them. Why not in the settings: see `assist/materials.py`.
    """

    # A switch, not deleting the files. Materials are prepared for one exam, but the assistant
    # is used between exams too: in an interview a lecture on databases is not needed and
    # costs thirty thousand tokens on every request.
    enabled: bool = True

    # A limit on how much goes to the model. Not about money: the cache makes a lecture nearly
    # free. It is about the fact that a person can upload a folder with a whole semester's
    # course and not notice, and along with it go the model's attention and seconds on the
    # first request, while the cache is written.
    #
    # 120 thousand characters is about thirty thousand tokens, roughly fifty pages. What did
    # not fit, the admin page shows as a number: cutting silently would be lying about what the
    # model answers from.
    max_chars: int = Field(default=120_000, ge=0, le=2_000_000)

    # A limit on pages that go as images: scans, photos, lectures without a text layer. A
    # separate number from `max_chars` because the price differs by an order of magnitude: a
    # page as an image is fifteen hundred tokens against three to four hundred for the same
    # page as letters.
    #
    # Forty pages is about sixty thousand tokens. On `anthropic-api` they are written to the
    # cache once and after that cost pennies; on the subscription they go out in full with
    # every question, and there forty pages is already a lot.
    max_pages: int = Field(default=40, ge=0, le=400)

    # Where to take the files from. Empty means the shared `materials/` folder next to
    # `argus.toml`. A chat (`argus/chats/`) has its own folder here, lying inside it: the
    # materials for one exam must not end up in a conversation about another. The path is
    # relative to the project root, like the shared folder itself.
    dir: str = ""


class Interface(BaseModel):
    """Language of the application itself: labels, buttons, hints.

    Page translation is done by `ui/i18n.py`: the dictionary of the chosen language is
    inserted into every HTML page, and the English string in the code serves as the key.
    "auto" is the Windows language, and if there is no translation for it, English.

    A temporary place. By meaning this is `ui.language` in the core: there is one interface
    for three channels, and keeping its language in the settings of one of them would give
    three answers to one question. It will move to the common config with the same merge as
    the rest.
    """

    language: str = "auto"


class AssistSettings(BaseModel):
    profile: Profile = Field(default_factory=Profile)
    vacancy: Vacancy = Field(default_factory=Vacancy)
    answering: Answering = Field(default_factory=Answering)
    overlay: Overlay = Field(default_factory=Overlay)
    hotkeys: Hotkeys = Field(default_factory=Hotkeys)
    prompting: Prompting = Field(default_factory=Prompting)
    materials: Materials = Field(default_factory=Materials)
    interface: Interface = Field(default_factory=Interface)


KEY = "assist"


_warned: set[str] = set()


def read(cfg) -> AssistSettings:
    """Pull the channel settings out of the free-form section of the config.

    Invalid values do not crash startup: the pipeline comes up on what was read, and the rest
    can be fixed in the same admin page. Dying at startup because of a setting you wrote
    yourself is the worst outcome.

    When one key is invalid, only that one is replaced with its default, not the whole
    section and not the whole block: otherwise an outdated value (`mic_mode`, which no longer
    exists) or a hand edit (`years = 61`) would erase the profile, the tone, the language, and
    the "What not to do" block from the prompt without a single word about
    it.
    """
    import logging

    raw = cfg.channel.get(KEY, {})
    try:
        return AssistSettings.model_validate(raw)
    except Exception as e:  # noqa: BLE001 — whatever happens, startup stays alive
        # It is read often (every state snapshot): the same trouble is written to the log once.
        said = _error_key(e)
        if said not in _warned:
            _warned.add(said)
            logging.getLogger(__name__).warning(
                "channel settings are invalid (%s): the invalid value was replaced by the default", said)
        return _salvage(raw)


def problems(cfg) -> list[str]:
    """A complaint for the problem list: part of the assistant settings could not be read. Empty
    means all is well.
    """
    try:
        AssistSettings.model_validate(cfg.channel.get(KEY, {}))
    except Exception:  # noqa: BLE001
        return ["part of the assistant's settings couldn't be read — the defaults are used instead"]
    return []


def _error_key(err: Exception) -> str:
    from pydantic import ValidationError

    if isinstance(err, ValidationError):
        return ", ".join(sorted(".".join(str(p) for p in e["loc"]) for e in err.errors()))
    return f"{type(err).__name__}: {err}"


def _salvage(raw) -> AssistSettings:
    """Everything that could be read: a bad field or section falls back to the default, the rest
    stays.
    """
    from pydantic import ValidationError

    if not isinstance(raw, dict):
        return AssistSettings()
    sections = {}
    for name, field in AssistSettings.model_fields.items():
        model, value = field.annotation, raw.get(name)
        if not isinstance(value, dict):
            continue
        value = dict(value)
        for _ in range(len(value) + 1):
            try:
                sections[name] = model.model_validate(value)
                break
            except ValidationError as err:
                bad = {str(e["loc"][0]) for e in err.errors() if e["loc"]} & value.keys()
                if not bad:
                    break               # the error is not about a single key — the whole section falls back to defaults
                for key in bad:
                    del value[key]
    return AssistSettings(**sections)
