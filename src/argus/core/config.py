"""Project settings: one `argus.toml` file instead of constants all over the code.

The defaults are set right here, in the models. `argus.toml` only overrides them, and
it is enough to write there what actually differs from the defaults, so you can see
exactly what you tweaked.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from argus.core.settings_store import decode_text, deep_merge, drop_invalid, read_overrides

log = logging.getLogger(__name__)

DEFAULT_CONFIG_NAME = "argus.toml"
ENV_NAME = ".env"

# A comment at the end of an unquoted value: a space and `#` ("KEY=abc # note").
# A `#` inside a value with no space before it ("abc#123") is part of the value.
_ENV_COMMENT = re.compile(r"\s#")


def _env_value(raw: str) -> str:
    """The value from a `.env` line: without quotes around it and without a trailing comment.

    In quotes, everything up to the closing quote counts, and whatever follows it
    (`"v" # note`) is dropped. Without quotes (or with an unpaired one), it stays as it
    was: spaces and quotes at the edges are stripped.
    """
    value = raw.strip()
    if value[:1] in ('"', "'"):
        end = value.find(value[0], 1)
        if end != -1:
            return value[1:end]
    cut = _ENV_COMMENT.search(value)
    if cut is not None:
        value = value[:cut.start()]
    return value.strip().strip('"').strip("'")


def load_env(path: Path, skip: Iterable[str] = ()) -> dict[str, str]:
    """Load the keys from a `.env` next to the config.

    Keys must not go into `argus.toml`, because it lives in the repository. But requiring
    an environment variable on every launch is also bad: it lives in one terminal, gets
    forgotten after a reboot, and its absence does not look like "no key" but like
    "the comparison somehow does not work".

    The `.env` file sits next to the project, in `.gitignore`, and is picked up by itself.

    An environment variable that is already set is **stronger than the file**: this way a
    temporary key in the terminal overrides the saved one, not the other way round. The
    reverse order would mean the file silently cancels what the person has just typed by
    hand.

    For a public build, the person creates the file by hand, with whatever tool is at
    hand: PowerShell 5.1 writes UTF-8 with a BOM or UTF-16, old Notepad writes ANSI with a
    comment that has non-ASCII letters. None of that may silently lose the first key (the
    BOM stuck to its name) or crash the launch: if the file is unreadable, there are no
    keys from it, and the log says so. `export KEY=v` and a comment after the value are
    understood.

    `skip` lists names we do not put into the environment: a key visible to the whole
    process would be inherited by a child process too (see `Config.load`).
    """
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
    except OSError as e:
        log.warning("cannot read %s (%s) — its keys were not taken", path.name, e)
        return {}
    try:
        text = decode_text(raw)
    except UnicodeError:
        # An ANSI file: the keys are ASCII and readable, while the comments in non-ASCII letters
        # are garbage that is skipped anyway.
        log.warning("not UTF-8: %s — reading as is, Cyrillic letters may be garbled", path.name)
        text = decode_text(raw, "replace")

    skipped = set(skip)
    taken: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export") and line[6:7].isspace():
            line = line[6:].lstrip()
        name, _, value = line.partition("=")
        name = name.strip()
        value = _env_value(value)
        if name and name not in skipped and not os.environ.get(name):
            try:
                os.environ[name] = value
            except ValueError:
                # A NUL in a name or value (a file in the wrong encoding): the environment does not
                # accept such a line, and the launch must not crash because of it.
                continue
            taken[name] = value
    return taken


class AudioConfig(BaseModel):
    """Audio capture.

    Devices may be left unspecified, and then the system defaults are used. If you give a
    string, it looks for a device whose name contains it; a number is the device index
    from `argus devices`.
    """

    mic_device: int | str | None = None
    loopback_device: int | str | None = None

    # Windows whose sound counts as "system sound". Empty means we take the whole device,
    # as before.
    #
    # The difference is not about convenience but about what lands in the timeline. The
    # device delivers everything at once: the interlocutor's speech, music, notifications, a
    # video in a neighboring tab. Recognition faithfully transcribes all of this mixed
    # together, and the model answers a song. The application delivers only its own sound.
    #
    # Several windows at once get a process each (`capture/audio.py: _AppChannel`), and the
    # chunks are published as they arrive, without being summed sample by sample. This is the
    # same compromise already accepted for several speakers in Discord
    # (`discord_bot/voice.py: MixSink`): with one active window there is no difference, and
    # with several people speaking at once recognition may degrade where they overlap, but
    # does not break entirely.
    #
    # They are matched by substring of the title, like the other pins: titles change
    # ("Zoom" -> "Zoom Meeting"), while window numbers live only until a restart.
    loopback_windows: list[str] = Field(default_factory=list)

    # The size of an audio chunk. 32 ms is a compromise: shorter causes extra context
    # switches, longer adds delay at the pipeline input.
    chunk_ms: int = Field(default=32, ge=8, le=200)

    # Whether to fold stereo into mono by averaging (otherwise the left channel is taken).
    downmix_average: bool = True

    @model_validator(mode="before")
    @classmethod
    def _migrate_single_window(cls, data: Any) -> Any:
        """The old field `loopback_window` (a single string): compatibility with already saved
        `argus.toml`/`argus.local.json` on other machines. Nothing writes it any more, but there is no
        point in rewriting other people's files just to rename a field."""
        if isinstance(data, dict) and "loopback_window" in data:
            data = dict(data)
            old = data.pop("loopback_window")
            if old and "loopback_windows" not in data:
                data["loopback_windows"] = [old]
        return data


class ScreenConfig(BaseModel):
    """Screen capture."""

    output_idx: int = 0

    # How often to poll DXGI. Ten times a second would be enough for reading text, but the
    # dashboard preview is limited by this same rate: it cannot show more than was captured.
    # Polling is almost free (18 thousand calls per second were measured on an unchanged
    # screen), so we take 30.
    #
    # This does not affect recognition: a filter stands in front of it, and the extra frames
    # never reach it.
    fps: int = Field(default=30, ge=1, le=60)

    # The screen region (left, top, right, bottom). None means the whole screen.
    region: tuple[int, int, int, int] | None = None

    # Frames per second in the dashboard preview. A frame costs about 4 ms, so 12 is roughly
    # 5% of one core. It does not affect what the model sees: one frame goes to it per call.
    preview_fps: int = Field(default=12, ge=1, le=30)

    # Window titles we do not offer for selection. The app's own dashboard comes first:
    # offering it would mean offering a loop.
    skip_windows: list[str] = Field(default_factory=lambda: ["Argus"])

    # The pinned window, by a piece of its title. Empty means the whole screen is read.
    #
    # There is deliberately no tracking of the active window: it meant that something random
    # was read (switched to mail, so we read mail). The person already knows what they want
    # to watch, and says so once.
    target_window: str = ""


class VADConfig(BaseModel):
    """Finding speech and cutting it into lines.

    The thresholds are tuned on a recorded session through `argus replay`, not by guess:
    a detector that is too sensitive chops a phrase into scraps, one that is too sluggish
    glues the lines of different people into one.
    """

    threshold: float = Field(default=0.5, ge=0.1, le=0.95)

    # How long speech must last for a phrase to begin. This cuts off clicks.
    min_speech_ms: int = Field(default=250, ge=50, le=2000)

    # How much silence counts as the end of a phrase. Under 400 ms it cuts at the pauses
    # between words, over 1000 it glues different lines together.
    min_silence_ms: int = Field(default=600, ge=100, le=3000)

    # Padding of audio before the start of speech: the detector fires on the first sound,
    # and without padding Whisper loses the first word.
    speech_pad_ms: int = Field(default=250, ge=0, le=1000)

    # The ceiling on line length. Whisper's window is 30 s, and it is better not to go
    # beyond it.
    max_utterance_s: float = Field(default=25.0, gt=1.0, le=30.0)


class STTConfig(BaseModel):
    """Speech recognition.

    There is no local engine in the product, and this is a decision about installation,
    not about quality. Whisper is one and a half gigabytes of weights that the user
    downloads on first launch, and one and a half gigabytes of video memory that they hold
    for the whole time it runs. Not everyone will install software that, after "download",
    asks for another one and a half gigabytes and a graphics card.

    Instead, recognition is entirely cloud-based and lives in `realtime_stt`.
    """

    provider: str = "realtime-openai"

    # None means detect from the audio. A specified language is noticeably more accurate and
    # faster.
    language: str | None = "ru"

    # Below this confidence a line does not go into the timeline: it is almost always an
    # invention on noise, and an invention is worse than a gap.
    min_confidence: float = Field(default=0.35, ge=0.0, le=1.0)

    # A second engine that listens to **the very same** audio for comparison.
    # None means do not compare.
    #
    # The mechanism remains, but for now there is nothing to compare against: the local
    # engine was removed and there is no second cloud one. We keep it for measuring when one
    # appears: both engines receive the same VAD segmentation, and only that way can they be
    # compared.
    compare_with: str | None = None


class RealtimeSTTConfig(BaseModel):
    """Recognition through the input transcription of a Realtime session, using a live
    session.

    It is not a transcriber in the old sense, and the difference is fundamental. An
    ordinary transcriber turns audio into text knowing only a language prior, and its
    single lever is a word list. Here the transcription is done by a model that has the
    accumulated conversation in front of it, so jargon is resolved from the meaning of what
    is going on. It cannot answer at all: input transcription (`audio.input.transcription`)
    is used, not a dialogue reply, and `response.create` is never sent after it.

    The session is kept open between lines for the sake of that context: the context
    accumulates in it by itself. Reconnecting wipes what has accumulated, and quality
    drops to exactly the level of an ordinary transcriber.
    """

    # The model of the Realtime session itself. It must be a dialogue model (`realtime`), not
    # a transcription one: it opens the session, while the input is listened to by a separate
    # model below (`transcribe_model`). They must not be mixed: the server flatly refuses if
    # a transcription model is in the URL.
    model: str = "gpt-realtime-mini"
    url: str = "wss://api.openai.com/v1/realtime"

    # The key is taken from the environment. It must not go into the config: the file lives
    # in the repository.
    api_key_env: str = "OPENAI_API_KEY"

    # Audio goes through the account server (`server/relay_stt.py`), not straight to the
    # cloud with a key. This is the only option for a distributed build: it has no key, and
    # the server counts the money. It works only when `[account] server` is set; without an
    # account (a personal build, replay) audio still goes with the key from the environment.
    relay: bool = False

    # The model that listens to the input; it does the transcription itself.
    # It is separate from `model` on purpose: `model` opens the dialogue session for the
    # sake of the accumulated context, while the transcription is done by the transcription
    # model inside it (`audio.input.transcription.model`).
    transcribe_model: str = "gpt-4o-mini-transcribe"

    # The "smart recognition" switch (UI label "Smart recognition") belongs to the person:
    # when off, `transcribe_model` listens; when on, `accurate_model` does. It is for a
    # strong accent, fast talk and unclear speech: the full model handles that better, but
    # costs more (its own rate in `server/pricing.py`) and may deliver text later. The
    # latency of the full model on a live line has not been measured yet.
    #
    # Changing it in the middle of a conversation reopens the session, and the accumulated
    # context is reset with it, as with a language change.
    smart: bool = False
    accurate_model: str = "gpt-4o-transcribe"

    # None means detect from the audio. A specified language is noticeably more accurate and
    # faster.
    language: str | None = "ru"

    # Words the model must spell correctly: terms, names, personal names. It goes into
    # `transcription.prompt`, a short vocabulary hint and not a role: this model has nothing
    # to answer with, so there is nothing to prop up with a ban on answering.
    #
    # Keep it short for the same reason as before: a long list starts to leak into the
    # transcription itself.
    terms: str = ""

    # How many of the latest items to keep in the session. The context is exactly what the
    # whole thing was started for, but the history is re-billed on every turn, so it cannot
    # be left unbounded.
    #
    # Items are counted, not turns, and now that is the same thing: input transcription adds
    # nothing of its own, it only labels an already existing audio item. So 20 items are the
    # last twenty lines, not ten as it would be with a dialogue reply next to them.
    keep_turns: int = Field(default=20, ge=0, le=200)

    # How long to wait for a transcription before counting the line as lost. The shadow
    # engine must not hang the pipeline if the network goes down.
    timeout_s: float = Field(default=15.0, gt=0)

    # Realtime accepts 24 kHz; everything inside our code is 16 kHz.
    target_rate: int = 24_000

    @property
    def active_model(self) -> str:
        """The model that listens to the input at the current position of the switch."""
        return self.accurate_model if self.smart else self.transcribe_model

    @property
    def tier(self) -> str:
        """The price name for expense accounting: "high" for smart recognition, otherwise "normal" (that
        is what the server calls them, `server/pricing.py`)."""
        return "high" if self.smart else "normal"


class GPTLiveTranscribeConfig(BaseModel):
    """Recognition through GPT-Live-Transcribe, a specialized model.

    It is not a dialogue session with transcription bolted on the side (as in
    `RealtimeSTTConfig`) but a separate session type, `session.type: "transcription"`,
    built specifically for transcribing. There is no conversational model here at all, so
    the worry "what if it answers instead of writing down" is removed structurally: this
    session type has physically nothing to answer with, and `response.create` does not
    exist as a concept.

    It still accumulates the conversation context by itself, just as the dialogue session
    did before: according to OpenAI, every next transcription takes into account the
    previous lines of the same session. Separate from that are the three manual levers
    below, `prompt`/`terms`/`language`, which can be updated during the conversation with
    a repeated `session.update`.

    Nothing in the protocol has been verified against the live API: when this code was
    written the model was new, there is no independent measurement of latency or accuracy,
    and the shape of `session.update` was assembled from the documentation, not from
    observation. On any schema mismatch the server replies with an `error` carrying the
    full text of the refusal; that is how to sort it out on the first live run.
    """

    url: str = "wss://api.openai.com/v1/realtime?intent=transcription"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "gpt-live-transcribe"

    # None means detect from the audio.
    language: str | None = "ru"

    # Free text: what the conversation is about and in what setting. It goes into
    # `transcription.prompt`, a hint about meaning and not a word list.
    prompt: str = ""

    # Words and names that must be spelled exactly as given. It goes into
    # `transcription.keywords`, a separate array from `prompt`; here it is comma-separated,
    # and it is split into items on commas before sending.
    terms: str = ""

    # A trade-off between how fast the text arrives and how accurate it is: `"minimal"`,
    # `"low"`, `"medium"`, `"high"`, `"xhigh"`. Empty means do not set it at all and leave
    # the decision to the server: this is one of the places not verified against the live API,
    # and fewer fields in the request means less chance of hitting a schema mismatch on the
    # first connection.
    delay: str | None = None

    # How many of the latest items to keep in the session: the same meaning and the same
    # number as `RealtimeSTTConfig.keep_turns`.
    keep_turns: int = Field(default=20, ge=0, le=200)

    timeout_s: float = Field(default=15.0, gt=0)

    # Realtime accepts 24 kHz; everything inside our code is 16 kHz.
    target_rate: int = 24_000


class DialogueTurn(BaseModel):
    """One line of a scenario (`StubSTTConfig.dialogue`): no audio, text only."""

    source: Literal["me", "them"]
    text: str


# The default interview: the interviewer starts, then they alternate. Your own scenario
# is a `[[stub_stt.dialogue]]` list in `argus.toml`, in place of this default in the code
# (see the comment there).
DEFAULT_DIALOGUE: list[DialogueTurn] = [
    DialogueTurn(source="them", text="Hello! Tell me about yourself."),
    DialogueTurn(source="me", text="Hello, my name is..."),
    DialogueTurn(source="them", text="What is the complexity of quicksort?"),
    DialogueTurn(source="me", text="On average O(n log n), in the worst case O(n²)."),
]


class StubSTTConfig(BaseModel):
    """A stub in place of recognition, see `perception/stt/stub.py`.

    Active only when `stt.provider = "stub"`.
    """

    # `{n}` is the line's sequence number, `{source}` is "me" or "them"; both are optional.
    # An empty string (after strip) publishes no line, just as a real engine discards
    # unrecognized silence.
    text: str = ""
    delay_ms: int = Field(default=0, ge=0)

    # A whole conversation script instead of microphone audio, see
    # `SpeechPipeline._run_dialogue`. It is separate from `text` above: the script gives
    # lines to both sides in turn, while `text` is only for incoming audio.
    dialogue_enabled: bool = False
    dialogue_interval_s: float = Field(default=4.0, gt=0)
    dialogue: list[DialogueTurn] = Field(default_factory=lambda: list(DEFAULT_DIALOGUE))


class GateConfig(BaseModel):
    """Filtering frames before recognition.

    DXGI already delivers a frame only when the picture changes, but for the driver a
    "change" is any single pixel: a blinking cursor, a ticking clock. One screen read costs
    about 400 ms, so a second filter is needed.
    """

    # How many of the 64 bits in a frame's fingerprint must change. 3 catches a window change
    # and big changes, but does not react to the cursor. One new line of text hardly changes
    # them (on a 2560x1440 screen, 0-1 bits): tiles stand next to them for local changes
    # (`perception/framegate.py`).
    min_distance: int = Field(default=3, ge=1, le=32)

    # No more than one recognition per this many seconds, even if the picture jumps around.
    # We keep a margin over the recognition time itself (~1.2 s on a dense screen):
    # otherwise the frames pile up in the queue and are dropped anyway, only after the work
    # has been wasted.
    min_interval_s: float = Field(default=3.0, ge=0.1, le=60.0)

    # Reread the screen even if it has not changed, when nothing has happened for a long time.
    heartbeat_s: float = Field(default=30.0, ge=1.0, le=600.0)


class OCRConfig(BaseModel):
    """Reading text from the screen.

    The model pair was chosen by measuring on an image with Russian, English and code.
    Version 5 of the `CH` detector groups text into lines, not into separate words, and for
    insertion into a prompt that matters more than the percentage of recognized characters.
    """

    provider: str = "rapidocr"

    # Whether to read the screen continuously.
    #
    # Turn it off where the screen is needed only occasionally and whole: the model gets the
    # picture anyway and reads it itself, and the recognized text is only needed as a memory
    # of what was on the screen earlier. If that memory is not needed, continuous reading
    # wastes 1.2 seconds of processor time on every noticeably changed frame. The screenshot
    # stays: reading is turned off, not capture.
    enabled: bool = True

    det_lang: str = "CH"
    det_version: str = "PPOCRV5"
    det_model: str = "MOBILE"

    # ESLAV stands for the East Slavic languages; it is trained on Cyrillic and Latin at once.
    rec_lang: str = "ESLAV"
    rec_version: str = "PPOCRV5"
    rec_model: str = "MOBILE"

    # Shrink the frame by width before recognition. A measurement on a dense IDE screen (the
    # worst case, 133 text blocks):
    #
    #      960 ->  736 ms,  24 blocks      1600 -> 1938 ms, 109 blocks
    #     1280 -> 1152 ms,  64 blocks      1920 -> 2361 ms, 124 blocks
    #                                      2560 -> 2415 ms, 133 blocks
    #
    # Time grows with the number of blocks found, because each is recognized separately.
    # 1280 is a compromise: large text (a question, a heading, a chat) is read reliably, and
    # only small interface detail is lost, which is noise for the model anyway. Raise it if
    # you need to read code in a small font.
    max_width: int = Field(default=1920, ge=0)

    # How many found regions to recognize at once. Also measured: 6 gives 2446 ms, while 32
    # gives 5530 ms on the same frame. More does not mean faster.
    rec_batch: int = Field(default=6, ge=1, le=32)

    # Blocks shorter than this are almost always interface fragments, not text.
    min_chars: int = Field(default=4, ge=1, le=20)

    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class BrainConfig(BaseModel):
    """Calling the model.

    The provider is switched here with one line. `claude-code` goes through a subscription
    and is the developer's working setup. `anthropic-api` and `openai-api` use a key, for
    your own machine and for comparing models with each other. `relay` is the same as
    `anthropic-api`/`openai-api` but without a key on the user's computer: the request goes
    to the account server (`[account] server`) and the key stays there (`server/relay.py`);
    this is what the distributed build uses. `local` is offline through an
    OpenAI-compatible endpoint. `stub` is a stub with no network and no money, see
    `brain/stub.py`.

    The default is `stub`, not `claude-code`: the developer's working setup (subscription,
    `claude` subprocess) must not switch itself on wherever `argus.toml` says nothing else.
    The product `argus.toml` sets `relay`, and a developer sets their own.
    """

    provider: str = "stub"

    # For relay: which upstream the account server calls; the same openai-api/anthropic-api
    # split as without the relay, only executed elsewhere.
    relay_upstream: str = "openai-api"

    # For stub: see `brain/stub.py`. `stub_answers` is not empty by default, so it is
    # noticeable that answers go round in a circle and do not repeat on every question.
    stub_text: str = "test answer"
    stub_answers: list[str] = Field(default_factory=lambda: ["one", "two", "three"])
    stub_delay_ms: int = Field(default=0, ge=0)

    # For claude-code: the CLI itself understands the short name.
    model: str = "sonnet"

    # For anthropic-api and openai-api: the full identifier of the active provider's model.
    # It is one and the same key for both: there is exactly one provider at a time, so there
    # is no reason to add a field for each.
    #
    # The default is the model the relay sells (`server/pricing.py: MODELS`) at the default
    # upstream (`relay_upstream`); otherwise a build without `argus.toml` would ask the relay
    # for something it does not sell. For `anthropic-api` you name your own model explicitly.
    api_model: str = "gpt-5.6-luna"
    effort: str = "medium"

    # The ceiling on an answer, in tokens. The schema allows up to 64 000; that is for your
    # own keys (`anthropic-api`, `openai-api`). Through the relay it can never exceed 16 000:
    # the server refuses a larger `max_tokens` (`server/relay.py: MAX_ANSWER_TOKENS`), so the
    # relay client itself clamps the value to that ceiling (`brain/relay_api.py`). Setting
    # more here is useless for the relay.
    max_tokens: int = Field(default=2000, ge=64, le=64_000)

    # "Think longer" (UI label "Think longer"), the second answer tier, in place of the
    # usual `api_model` + `effort`. It is not a filter's decision but a person's manual
    # request (a button on the console), for a question where a second or two of waiting
    # matters less than a stronger answer. Empty means the second tier has no model of its
    # own, and the effort is simply raised on the same model.
    deep_model: str = ""
    deep_effort: str = "high"

    # How long the cache of the system part lives on the API side.
    #
    # An hour, not the default five minutes, and this is about pauses. The five-minute cache
    # holds by itself while questions follow one another: each call moves the expiry again.
    # But as soon as the person stops to think for ten minutes, the next question pays for a
    # full prefill, an extra second exactly where a second is visible. In a conversation such
    # pauses are the norm, not the exception.
    #
    # The price: a write to the hour cache costs twice the normal input, to the five-minute
    # one a quarter more. Reading either costs a tenth. So the hour pays off from the third
    # call, the five-minute one from the second; at our number of questions per session the
    # difference between them gets lost.
    cache_ttl: Literal["5m", "1h"] = "1h"

    # For local: the address of the OpenAI-compatible endpoint.
    local_url: str = "http://localhost:1234/v1"
    local_model: str = "local-model"

    timeout_s: float = Field(default=60.0, gt=1.0)

    # How many characters of the timeline to put into the prompt. An hour of conversation is
    # about 30 thousand characters, so 12 thousand is roughly the last twenty minutes.
    max_context_chars: int = Field(default=12_000, ge=500)

    # Send the model the screen picture itself, not only the recognized text.
    #
    # On by default: text alone is not enough. OCR pulls out lines, and everything else is
    # lost: what is drawn, where things are, what is happening in a game or on a diagram. A
    # measurement on a live screen, the same question: without the picture the model
    # answered "not enough data", with the picture it worked out that the screen shows a page
    # with a connection error.
    #
    # The price: roughly 1000-1500 tokens against a hundred for text, and +220 ms. On a
    # subscription this is a noticeable use of the limit, so the switch is kept, but by
    # default the model looks at the whole screen.
    send_screenshot: bool = True

    # When exactly to attach it.
    #
    # `always` means with every call. `ask` means only with "Ask about the screen"
    # (`ask_about_screen`, hotkey `ask_screen`): the plain "Ask" carries
    # no frame. `ask` is for conversational channels where most questions are spoken ("tell
    # me about the GIL"): the picture answers nothing for them, yet it costs 1000-1500 tokens
    # and +220 ms on every question. The person knows when something worth looking at appears
    # on the screen, and presses the button.
    screenshot_mode: Literal["always", "ask"] = "always"

    # The width of the picture before sending. More than 1024 hardly helps the model, but
    # eats noticeably more tokens.
    screenshot_width: int = Field(default=1024, ge=256, le=2048)

    screenshot_quality: int = Field(default=72, ge=30, le=95)

    # Two variants, not one: the prompt talks about the screenshot only when it is actually
    # attached to the call. `Assistant._ask` chooses between them by whether a picture is
    # attached to this particular question; see `default_system` in `brain/assistant.py`.
    system_prompt: str = ""
    system_prompt_with_image: str = ""


class BrainGateConfig(BaseModel):
    """When to wake the model.

    The main protection of the subscription limits: they are counted in five-hour windows,
    and a pipeline that pokes the model on every line would burn through them in an hour.
    """

    # The minimum idle time between calls.
    cooldown_s: float = Field(default=8.0, ge=0.0, le=600.0)

    # A per-session ceiling, insurance against an endless loop.
    max_calls: int = Field(default=200, ge=1, le=10_000)

    min_question_chars: int = Field(default=12, ge=1, le=200)

    # Triggering on silence. 0 means off.
    idle_s: float = Field(default=0.0, ge=0.0)

    screen_new_lines: int = Field(default=4, ge=1, le=100)

    # Off by default: the screen is noisier than speech, and reacting to it would quickly eat
    # the limits on trifles.
    react_to_screen: bool = False

    # mic_mode/system_mode and wake_word are deliberately not here: they are
    # `Answering.mic_mode`/`.system_mode`/`.wake_word` in assist/settings.py, controls for a
    # specific conversation and not for the engine.


class RecordConfig(BaseModel):
    """Recording sessions to disk.

    The main cost is the screen frames. One 2560x1440 frame as JPEG weighs ~150-280 KB,
    and with an active picture hundreds of megabytes pile up in five minutes. The disk on
    this machine is tight, so both the quality and the size of the frame are exposed as
    settings.
    """

    jpeg_quality: int = Field(default=70, ge=30, le=100)

    # Shrink the frame by width before recording; 0 means record as is.
    #
    # 1280 px is what the frame is shrunk to anyway before the screen is read, and the model
    # gets a picture of the same size. Recording 2560 would mean storing twice as many pixels
    # that nobody will ever look at.
    max_frame_width: int = Field(default=1280, ge=0)

    # Warn when a recording grows large. It does not stop anything, it just says so.
    warn_size_mb: int = Field(default=1024, ge=0)

    # Record the session always, without the `--record` flag.
    #
    # The point is that a recording cannot be made after the fact. An interesting session is
    # recognized as interesting only after it has passed: the engine got an unfamiliar word
    # wrong, the model answered off the mark, and the audio is already gone, so there is
    # nothing to check a fix on. A flag that has to be remembered in advance does not solve
    # this: it gets remembered exactly when nothing is going on.
    always: bool = False

    always_screen: bool = True

    # How often to record frames. Capture runs at 30 frames per second, and there is no
    # reason to write them all: a gigabyte per ten minutes goes to saving the same picture
    # thirty times.
    #
    # The recording exists so that you can later ask the model "what is happening on the
    # screen". For such a question one frame per second is more than enough: in that time the
    # situation does not change enough to make the answer different.
    frames_fps: float = Field(default=1.0, gt=0, le=30)

    # A ceiling on all recordings together. When the sum exceeds it, the oldest are deleted
    # until it fits.
    #
    # Audio does not "weigh almost nothing", as it seems: 16 kHz int16 is 31 KB/s per
    # channel, that is **220 MB per hour** for both. Constant recording with no ceiling would
    # eat 33 free gigabytes in a week of gaming, and it would be discovered only when the
    # disk ran out in the middle of a session. 0 means no limit.
    keep_gb: float = Field(default=8.0, ge=0)


class AccountConfig(BaseModel):
    """Account and balance: we pay for usage, and the money is kept on the server.

    Everything that decides about money and access is computed by the server; only its
    address is here. There are deliberately no checks inside the app: it is distributed to
    people, and any such check is cut out in ten minutes. The one thing that cannot be cut
    out is the server's refusal to hand over the model's answer.

    An empty address means "no server": the account section is not shown in the settings,
    and the app works on its own keys, as before. That is what development looks like, and
    also a personal build for yourself.
    """

    server: str = ""


class UpdateConfig(BaseModel):
    """Auto-update of the built `.exe`; not for development.

    An empty address means "nothing to update from": a working copy reads `src/` and
    updates through `git pull`, and does not need this. It is filled in only for the built
    app (`sys.frozen`): on every launch it asks `{server}/latest.json`, and if the version
    there is newer than the one recorded in `VERSION` next to the `.exe` (put there by
    `packaging/stage.py`), it downloads the new archive and replaces itself, see
    `update.py`.
    """

    server: str = ""


class LegalConfig(BaseModel):
    """A recording notice before recognition is first turned on.

    The terms and the privacy policy have no settings: their texts live in `legal/`, are
    built into the app (`ui/static/legal-*.html`) and open inside it (`account/legal.py`).
    """

    # A one-time notice before recognition is first turned on: what is recorded and that the
    # interlocutors may have a right to know about it. It can be turned off only in a personal
    # build for yourself; in a distributed one it must stay on.
    notice: bool = True


class UIConfig(BaseModel):
    """Dashboard and admin panel.

    The port lives in the settings, not in a command argument, and this follows from how
    the project is organized: three working copies, three branches, and all three start the
    same `argus run`. There is one port for all, so the three copies cannot start together,
    and explaining that with a flag at every launch means forgetting it one day. Each copy
    writes its port once and no longer thinks about it.
    """

    port: int = Field(default=8420, ge=1024, le=65535)

    # Whether to show the settings page. By default (without `argus.toml`) it is off. The
    # built app takes the value from the `argus.toml` that `packaging/stage.py` puts in place,
    # and there the page is on now: in the window it is "settings" (the gear) with tabs, not a
    # scatter of technical knobs.
    #
    # A disabled page rests on the idea that technical knobs (model, thresholds, limits,
    # port) are of no use to a person who does not understand their cost. It is turned off
    # with one line: `[ui] admin = false`.
    admin: bool = False

    # Loopback only. The dashboard shows the whole screen and conversation, so it must not be
    # exposed to the network, and it has no password and never will. That is why the value is
    # checked: `0.0.0.0` or an address on the local network would give everyone on the same
    # network access to the screen, the conversation and the paid assistant.
    host: str = "127.0.0.1"

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        host = value.strip()
        if host.lower() == "localhost":
            return host
        try:
            if ipaddress.ip_address(host).is_loopback:
                return host
        except ValueError:
            pass
        raise ValueError("the dashboard listens only on this computer: 127.0.0.1, ::1 or localhost")

    def base_url(self, port: int = 0) -> str:
        """Dashboard address: `http://host:port`. An IPv6 address (`::1`) goes in brackets: without them
        the port merges with the address, and `http://::1:8420` opens neither in the window nor in the
        browser."""
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{port or self.port}"

    # Show the interface in its own window, not in a browser tab.
    #
    # The address is the same: the window is WebView2 on top of the same local server, and
    # there is no second implementation of the interface. The difference is in how it looks:
    # a tab has an address bar that shows up in screen sharing, neighboring tabs, and the risk
    # of closing it by accident. The browser stays at hand for debugging: `argus run --browser`.
    window: bool = True

    # Hide the window from screen capture.
    #
    # `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`, Win10 2004+: the window is visible to
    # the person at the monitor and appears neither in screen sharing nor in a screenshot.
    # The mechanism is common; password managers and players use it.
    #
    # On by default (since 2026-10-14): the point of the product is an assistant that the
    # interlocutor does not see, and a forgotten checkbox would give it away in the very first
    # screen sharing. It is turned off explicitly: `[ui] exclude_from_capture = false` or the
    # "Invisible in recordings" switch in the settings window.
    #
    # It hides from capture, not from eyes: a camera pointed at the monitor and a person
    # nearby see everything as it is.
    exclude_from_capture: bool = True

    # The main window's button on the taskbar and its row in Alt+Tab (Windows).
    #
    # The taskbar is part of the shared screen: a window that is invisible on the recording
    # but has an "Argus" button next to it gives away that the app is open. Detached panels
    # never have a button; the main window has one by default (`true`), because it is the only
    # way back to a minimized window. With `false` the window becomes a tool window
    # (`WS_EX_TOOLWINDOW`), with no button and no row in Alt+Tab. It can be brought back with
    # the "Open/close main window" shortcut or by starting
    # the program a second time (that shows the hidden window). "Minimize" in such
    # a window hides it the same way. The switch in the settings window takes effect at once.
    # It does nothing on macOS.
    main_taskbar_button: bool = True

    # A transparent background for detached panels: you can see through the window to what
    # lies under it, while the text stays sharp (Windows 11 22H2+, see `desktop._glass`). On
    # older builds and with `false` the panel is opaque. There is no overall window
    # transparency (a slider on the strip) in either view.
    panel_glass: bool = True

    # macOS: the app has no Dock icon and no menu bar (an "agent"), so that the panels can sit
    # on top of another program's full-screen window, such as a call or a presentation. macOS
    # does not allow this for an ordinary app (the system does not layer its windows over
    # another program's full-screen space; confirmed by an Apple engineer and verified on real
    # macOS 26 on 2026-10-13: without the mode the panel is not visible behind a full-screen
    # Safari, with it the panel stays on top), but it does allow it for an "agent".
    #   auto    - the app becomes an agent while a panel is open, and gets the icon back when
    #             it is closed;
    #   always  - always: no icon and no menu; open the program from Applications or Spotlight;
    #   off     - never: the panels are visible on the desktop, but not over another program's
    #             full-screen window.
    # `true`/`false` from earlier settings mean "always"/"off". It does nothing on Windows.
    mac_overlay: Literal["auto", "always", "off"] = "auto"

    @field_validator("mac_overlay", mode="before")
    @classmethod
    def _overlay_from_bool(cls, value: Any) -> Any:
        if value is True:
            return "always"
        if value is False:
            return "off"
        return value

    window_title: str = "Argus"
    window_width: int = Field(default=1280, ge=640, le=3840)
    window_height: int = Field(default=860, ge=480, le=2160)
    # Opens already maximized (see `desktop.py: run_desktop`); the width and height above are
    # not superfluous: that is the size the window returns to when it is restored with the
    # button in the frame. It can be turned off here without touching the code.
    window_maximized: bool = True


class PathsConfig(BaseModel):
    """Where to put session recordings, model weights and chats."""

    sessions: Path = Path("sessions")
    models: Path = Path("models")
    # Chats: each has its own folder with settings, answer history and materials
    # (`argus/chats/`). This is personal data, like `argus.local.json`: it does not go into
    # the repository and an update does not touch it.
    chats: Path = Path("chats")


class Config(BaseModel):
    audio: AudioConfig = Field(default_factory=AudioConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    vad: VADConfig = Field(default_factory=VADConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    realtime_stt: RealtimeSTTConfig = Field(default_factory=RealtimeSTTConfig)
    gpt_live_transcribe: GPTLiveTranscribeConfig = Field(default_factory=GPTLiveTranscribeConfig)
    stub_stt: StubSTTConfig = Field(default_factory=StubSTTConfig)
    gate: GateConfig = Field(default_factory=GateConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    gate_brain: BrainGateConfig = Field(default_factory=BrainGateConfig)
    record: RecordConfig = Field(default_factory=RecordConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    account: AccountConfig = Field(default_factory=AccountConfig)
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    legal: LegalConfig = Field(default_factory=LegalConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    # Channel settings: what the core does not have and must not have.
    #
    # Three branches share the core and differ by channel: a candidate's resume, the name of a
    # Twitch channel, the synthesizer voice. Adding a field in the shared config for each would
    # mean dragging into master what exactly one branch needs, and not adding any would mean
    # the channel's admin panel has nowhere to keep its own.
    #
    # So here is a free-form section: the core only carries it from the file and back without
    # looking inside. The branch parses it, with its own model, in its own module, with its
    # own value validation.
    channel: dict[str, Any] = Field(default_factory=dict)

    # The project root; relative paths are resolved from it.
    root: Path = Field(default_factory=Path.cwd, exclude=True)

    # Where `argus.toml` was read from. The admin panel needs it: it writes edits to a file
    # next to it and must know what to count as the initial state.
    config_path: Path | None = Field(default=None, exclude=True)

    @classmethod
    def load(cls, path: Path | None = None, *, overrides: bool = True) -> Config:
        """Read the config. If there is no file, return the defaults.

        The admin panel's edits (`argus.local.json`) are layered on top of the file. This order
        is deliberate: `argus.toml` holds what was decided about the project and explained in
        comments; the local file holds what a person tweaked for themselves on this machine.
        `overrides=False` gives "as in the repository"; the admin panel itself uses it to show
        what it has overridden.
        """
        if path is None:
            path = find_config()
        if path is None or not path.exists():
            return cls()
        with path.open("rb") as f:
            base = tomllib.load(f)
        root = path.parent
        data = base
        local: dict[str, Any] = {}
        if overrides:
            local = read_overrides(root)
            data = deep_merge(base, local)
        try:
            cfg = cls.model_validate(data)
        except ValidationError:
            if not overrides:
                raise
            # A bad value in `argus.local.json` (a release narrowed what is allowed, a slip by hand)
            # must not crash the launch with a "Failed to start" window: we
            # remove only that value and the other edits stay. We do not touch the file itself: it
            # belongs to the person.
            cleaned = drop_invalid(base, local)
            if cleaned is None:
                # Without edits, the file to blame is not theirs but `argus.toml` itself, and the error
                # below will say so.
                if local:
                    log.warning("edits from argus.local.json are not valid as a whole - reading argus.toml")
                cleaned = {}
            data = deep_merge(base, cleaned)
            cfg = cls.model_validate(data)    # will fail again only if `argus.toml` itself is bad
        unknown = unknown_keys(data)
        if unknown:
            log.warning(
                "settings contain keys that the program does not have (typo?): %s",
                ", ".join(unknown),
            )
        cfg.root = root
        cfg.config_path = path
        # We do not hand the process the Anthropic key from `.env` if the model goes through a
        # subscription (`claude-code`): the `claude` child process inherits the environment and,
        # finding a key there, would prefer it to the subscription, and the spending would go to
        # pay-per-key billing.
        load_env(
            cfg.root / ENV_NAME,
            skip=("ANTHROPIC_API_KEY",) if cfg.brain.provider == "claude-code" else (),
        )
        return cfg

    def model_post_init(self, _: object) -> None:
        # The default prompt lives next to the logic and is not duplicated in the config. There
        # are two variants, a picture attached to the call or not, see `default_system` in
        # `brain/assistant.py`.
        if not self.brain.system_prompt or not self.brain.system_prompt_with_image:
            from argus.brain.assistant import default_system

            if not self.brain.system_prompt:
                self.brain.system_prompt = default_system(False)
            if not self.brain.system_prompt_with_image:
                self.brain.system_prompt_with_image = default_system(True)

    def resolve(self, p: Path) -> Path:
        """Expand a relative path from the project root."""
        return p if p.is_absolute() else (self.root / p)

    @property
    def sessions_dir(self) -> Path:
        return self.resolve(self.paths.sessions)

    @property
    def models_dir(self) -> Path:
        return self.resolve(self.paths.models)

    @property
    def chats_dir(self) -> Path:
        return self.resolve(self.paths.chats)


def find_config(start: Path | None = None) -> Path | None:
    """Find argus.toml in the current folder or higher up the tree."""
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        candidate = d / DEFAULT_CONFIG_NAME
        if candidate.exists():
            return candidate
    return None


# Former key names that the model still understands by itself
# (`AudioConfig._migrate_single_window`): a rename must not look like a typo.
_LEGACY_KEYS = frozenset({"audio.loopback_window"})


def unknown_keys(
    raw: dict[str, Any], model: type[BaseModel] | None = None, prefix: str = ""
) -> list[str]:
    """Keys of the raw settings that the models do not have, as full paths
    ("gate_brain.cooldown").

    The models silently skip extras: that is how an older program starts on a file from a
    newer one. But a typo (`cooldown` instead of `cooldown_s`) looks like a setting that
    "does nothing", and there is nothing to find it with. Startup (a line in the log) and
    `argus doctor` use this. The free-form `channel` section is parsed by the channel
    branch, and we leave it alone.
    """
    model = model or Config
    out: list[str] = []
    for key, value in raw.items():
        path = f"{prefix}{key}"
        field = model.model_fields.get(key)
        if field is None:
            if path not in _LEGACY_KEYS:
                out.append(path)
            continue
        try:
            nested = isinstance(value, dict) and issubclass(field.annotation, BaseModel)
        except TypeError:               # `dict[str, Any]` and other non-classes
            nested = False
        if nested:
            out.extend(unknown_keys(value, field.annotation, f"{path}."))
    return out


def raw_settings(path: Path | None = None) -> dict[str, Any]:
    """`argus.toml` with the overrides applied: as a raw dictionary, the way it goes into the model."""
    path = path or find_config()
    if path is None or not path.exists():
        return {}
    with path.open("rb") as f:
        return deep_merge(tomllib.load(f), read_overrides(path.parent))
