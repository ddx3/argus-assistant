"""The speech synthesis hook: a separate subscription to `Answer`, not a provider wrapper.

`Bus.publish()` sends the same `Answer` object to all subscribers of its type
(`core/bus.py`), so it can be listened to independently of `app._wire()` without touching
anything there. `Answer.meta` is an ordinary dict, and `audio_ids` can be appended to it
after the answer text has already gone to the overlay: the text appears at once and the
sound follows, not the other way around, as it would be with a wrapper over
`LLMProvider.ask()`.

The answer is spoken sentence by sentence, not in one piece. Synthesizing a whole long
answer means waiting for the whole answer before a single word is heard; splitting with
`text.split_sentences()` gives a pipeline: the first phrase is already playing while the
second is still being synthesized. `audio_ids` grows as it goes, and the frontend queues
the ready pieces as they appear instead of waiting for the whole list at once.

With a streaming provider (`ask_stream` in `brain/openai_api.py`), the pipeline starts
earlier: `Assistant._ask` publishes `Answer` to the bus at once, empty (`done=False`), and
grows its `text` in place as the model answers. `_speak` below watches that growth itself
and speaks finished sentences, without waiting not only for the whole answer but even for
the end of text generation.

`brain/assistant.py` and `app.py` do not know this module at all: all enabling and
disabling lives here and in two calls from `ui/admin.py`. Removing the feature means
deleting the `tts/` folder and those two calls, without touching the core.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from argus.brain.base import Answer
from argus.core import friendly
from argus.tts import client
from argus.tts.settings import TTSSettings
from argus.tts.settings import read as tts_read
from argus.tts.text import speakable, split_sentences, take_ready_sentences

if TYPE_CHECKING:
    from argus.app import Argus
    from argus.core.bus import Subscription

log = logging.getLogger(__name__)

# How many of the latest clips to keep in memory. One reply is now several clips (one per
# sentence) instead of one, so the cap is higher than the number of clips that a
# `deque(maxlen=20)` of replies in `app.py` would hold with a one-to-one split.
CLIP_LIMIT = 80

# The phrase for the "Play a sample" button in the admin page. A cloned voice is
# checked before voice-over for replies is turned on at all, so this is neutral text, not
# a line from a real conversation.
PREVIEW_TEXT = "Hello! I am checking how this voice sounds."

# How often to re-read the growing `answer.text` while a streamed reply is not yet
# complete (`answer.done=False`). A one-second delay is invisible against synthesis,
# because synthesizing a single sentence in ElevenLabs takes hundreds of milliseconds on
# its own. Polling too often would spin idle while no text has been added.
POLL_S = 0.1

# What to tell the person when voice-over is on but there is no voice (`Watcher.problem`).
# It names no services or keys, because it lands in the complaints strip next to sound
# and screen.
SIGN_IN = "sign in to your account to get the voice working"
NOT_SET_UP = "the voice is not set up"

# Retrying right away after an access or balance refusal (401/402/403) is pointless: every
# following reply would knock and get the same refusal. We wait until the settings or the
# sign-in change, but no longer than this: the balance may have been topped up while the
# settings stayed the same.
HALT_S = 300.0

# A trial phrase costs money, so we allow no more than one per this interval. Otherwise a
# browser tab hitting `/tts/preview` in a loop would drain the person's balance.
PREVIEW_GAP_S = 2.0
PREVIEW_BUSY = "The sample phrase is still playing or has just played — wait a couple of seconds."

# These phrases (`friendly`) are transient problems: network, overload. Any other server
# refusal is about access or balance, and there is no point retrying it until the
# settings change.
_TRANSIENT = frozenset({
    friendly.NETWORK, friendly.TIMEOUT, friendly.BUSY, friendly.DOWN, friendly.OTHER,
})


class Watcher:
    """Listens to answers, synthesizes speech sentence by sentence, remembers recent clips."""

    def __init__(self, app: Argus, sub: Subscription, settings: TTSSettings) -> None:
        self.app = app
        self.sub = sub
        self.settings = settings
        self.clips: dict[str, bytes] = {}
        self._order: list[str] = []
        # Serial, not parallel: two chunks (let alone two different replies) voiced at the same
        # time would overlap each other in the overlay. It has no playback queue of its own, so
        # this lock keeps the order.
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        # The voice-overs in progress. `stop()` cancels them; otherwise synthesis that nobody
        # needs anymore (voice-over was switched off, the chat changed) would be finished and
        # paid for to the end.
        self._tasks: set[asyncio.Task] = set()
        # The last failure in words for the person; empty means everything is fine (`problem`).
        self.last_error = ""
        self._halt_key: object | None = None
        self._halt_until = 0.0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for task in list(self._tasks):
            task.cancel()
        self.sub.close()

    async def _run(self) -> None:
        async for answer in self.sub:
            # Every reply is its own task. While one is being synthesized under the lock, the next
            # one already waits its turn instead of blocking the bus reader (otherwise
            # `Subscription._offer` would pile up `Answer` objects and could start dropping them on
            # overflow).
            task = asyncio.create_task(self._speak(answer))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # -- state ------------------------------------------------------------

    def problem(self) -> str:
        """Why voice playback does not sound when it is enabled; empty means everything is fine. For
        the complaints strip.
        """
        return f"voice replies: {self.last_error}" if self.last_error else ""

    def _current(self, answer: Answer) -> bool:
        """The answer is still worth voicing: voice playback is on and the conversation has not
        changed. The answer of a closed chat (same `epoch`) is read by no one any more — its
        sound would play in the middle of a new one."""
        if not self.settings.enabled:
            return False
        assistant = getattr(self.app, "assistant", None)
        epoch = answer.meta.get("epoch")
        return assistant is None or epoch is None or epoch == assistant.epoch

    def _state_key(self, settings: TTSSettings) -> tuple:
        """What a refusal depends on: the settings and the sign-in. If they changed, it is worth
        trying again.
        """
        account = getattr(self.app, "_account", None)
        return (settings, bool(getattr(account, "signed_in", False)) if settings.relay else None)

    def _halted(self, settings: TTSSettings) -> bool:
        if self._halt_key is None:
            return False
        if time.monotonic() >= self._halt_until or self._halt_key != self._state_key(settings):
            self._halt_key = None
            return False
        return True

    def _failed(self, error: Exception, settings: TTSSettings) -> None:
        """Remember a synthesis failure: the person gets a phrase in the complaints, and a refusal
        over access or balance also stops retries until the settings or the sign-in change
        (`HALT_S`)."""
        self.last_error = friendly.explain(error)
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status in (401, 402, 403) or self.last_error not in _TRANSIENT:
            self._halt_key = self._state_key(settings)
            self._halt_until = time.monotonic() + HALT_S

    async def _speak(self, answer: Answer) -> None:
        settings = self.settings
        if not settings.enabled:
            return
        if not self._current(answer):
            return      # the conversation is different now (another chat was opened): this reply belongs to the past
        if answer.meta.get("silent"):
            # A service card (conversation recap, "I am watching …", `argus/recap/`). It is
            # long, voice-over is paid per character, and nobody asked to read it aloud in the middle
            # of a conversation.
            return

        profile = settings.characters.get(settings.active_character)
        if profile is None or not profile.voice_id:
            return

        api_key = ""
        if settings.relay:
            account = getattr(self.app, "_account", None)
            if account is None or not account.signed_in:
                self.last_error = SIGN_IN
                log.warning("speech via the relay is enabled, but there is no account sign-in — synthesis skipped")
                return
        else:
            api_key = os.environ.get(settings.api_key_env, "").strip()
            if not api_key:
                self.last_error = NOT_SET_UP
                log.warning(
                    "speech is enabled, but empty: %s — synthesis skipped", settings.api_key_env
                )
                return
        if self._halted(settings):
            return      # recent access or balance refusal: while nothing has changed, we do not knock again

        base = answer.meta.get("t")
        if base is None:
            # Without `t` there is nothing to address the clips by. Such a reply should not exist
            # (`Assistant._ask` always sets it, even for a streamed reply, before publishing to the
            # bus), but there is no reason to crash over someone else's missing field.
            return

        # A list, not a counter: the frontend polls the whole `Answer` (a snapshot every 0.5 s)
        # and must see each new chunk exactly once. A separate list, rather than a "how many are
        # ready" count, gives it that without a race.
        answer.meta["audio_ids"] = []
        idx = 0

        if answer.done:
            # It arrived whole (an ordinary, non-streaming provider): the old logic, unchanged.
            # `split_sentences` glues short fragments to their neighbors, which is only right when
            # the whole text is visible at once.
            for sentence in split_sentences(speakable(answer.text, settings.max_chars)):
                if not await self._speak_one(answer, base, idx, sentence, profile, settings, api_key):
                    return
                idx += 1
            return

        # The reply is still streaming (`Assistant._ask`, a streaming provider). We poll the
        # growth of `answer.text`, the same idea as `audio_ids` on the other end: a growing
        # snapshot, not an event per chunk. We voice complete sentences as they appear, so the
        # first sound does not wait for the whole reply. We use `take_ready_sentences`, not
        # `split_sentences`: short fragments are NOT glued to their neighbors here. On a stream
        # the neighbor is not there yet, and waiting for it to glue would slow down exactly what
        # we stream for in the first place.
        #
        # We look for boundaries in the RAW `answer.text`, not in the result of `speakable`, and
        # `consumed` is an offset in that same raw text. Before, `speakable` was re-run on the
        # whole accumulated text at every poll, and `consumed` counted a position in ITS output.
        # But `speakable` is not idempotent on a growing prefix: it leaves `**bold` alone when
        # the second `**` has not arrived yet, and once the pair arrives in the next piece, the
        # whole "bold" fragment collapses and shifts everything after it. A `consumed` counted
        # on the old layout then cut at the wrong positions: it either silently lost pieces of
        # text or found no new sentences at all until the very end of the reply. The raw text
        # only grows, by appending, and an offset in it does not wander. We clean the markdown
        # only after a chunk has been found, one chunk at a time. If the markup does fall
        # exactly on a boundary between chunks, then at worst one character of the sound is
        # spoiled, not the whole voice-over at once.
        consumed = 0
        budget = settings.max_chars  # the same overall cap per reply as on the done path above
        while True:
            if not self._current(answer):
                return      # voice-over was switched off or the conversation changed: no point waiting for the end of the reply
            done = answer.done
            pieces, upto = take_ready_sentences(answer.text[consumed:], final=done)
            consumed += upto
            for raw_piece in pieces:
                if budget <= 0:
                    return
                sentence = speakable(raw_piece, budget)
                if not sentence:
                    # The chunk went entirely into cleaning (code, a formula with no text around it). There
                    # is nothing to voice, but `idx` does not pay for it either: it addresses only the clips
                    # that were actually sent to synthesis.
                    continue
                if not await self._speak_one(answer, base, idx, sentence, profile, settings, api_key):
                    return
                budget -= len(sentence)
                idx += 1
            if done:
                return
            await asyncio.sleep(POLL_S)

    async def _speak_one(
        self, answer: Answer, base: object, idx: int, sentence: str, profile, settings, api_key: str,
    ) -> bool:
        """Synthesize one chunk, save it, and append it to `audio_ids`.

        `settings` is the same snapshot that `_speak` captured at the start of the reply, not
        the current `self.settings`. If the admin page applies new settings (`install()`
        replaces them wholesale) while this reply is still being voiced chunk by chunk, the
        later chunks must not silently drift onto a different `model_id`/`output_format` than
        the voice that was chosen for the old reply.

        `False` means the network or the limits refused. The caller stops hammering the rest of
        this reply, because the network has already failed once.
        """
        clip_id = f"{base}-{idx}"
        async with self._lock:
            # Under the lock, not before it. While the previous chunk (or reply) was being voiced,
            # voice-over could have been switched off and the chat changed, so paid synthesis of this
            # chunk is no longer needed.
            if not self._current(answer):
                return False
            try:
                if settings.relay:
                    account = getattr(self.app, "_account", None)
                    audio = await client.synthesize_via_relay(sentence, profile, settings, account)
                else:
                    audio = await client.synthesize(sentence, profile, settings, api_key)
            except Exception as e:  # noqa: BLE001 — network/limit errors must not take down the pipeline
                log.warning("speech synthesis failed (chunk %d): %s", idx + 1, e)
                self._failed(e, settings)
                return False
        self.last_error = ""
        self._store(clip_id, audio)
        # A new list on the object, not an in-place `.append()`. The state snapshot is read
        # concurrently (the websocket broadcast, every 0.5 s). Swapping a reference is atomic in
        # CPython, but mutating a list that has already been handed out is not.
        answer.meta["audio_ids"] = [*answer.meta["audio_ids"], clip_id]
        return True

    def _store(self, clip_id: str, audio: bytes) -> None:
        self.clips[clip_id] = audio
        self._order.append(clip_id)
        while len(self._order) > CLIP_LIMIT:
            oldest = self._order.pop(0)
            self.clips.pop(oldest, None)


def install(app: Argus, settings: TTSSettings) -> Watcher | None:
    """Idempotently create, update, or switch off voice-over on a running app.

    A second call does not create a second bus subscription; it updates the settings of
    the `Watcher` that is already alive. `enabled=False` stops the existing one and creates
    nothing new.
    """
    existing: Watcher | None = getattr(app, "_tts_watcher", None)

    if not settings.enabled:
        if existing is not None:
            existing.stop()
            app._tts_watcher = None
        return None

    if existing is not None:
        if existing.settings != settings:
            # Different settings are a different reason to try: the earlier refusal and the stop of
            # retries (`_halted`) no longer apply to them.
            existing.last_error = ""
            existing._halt_key = None
        existing.settings = settings
        return existing

    sub = app.bus.subscribe(Answer, maxsize=16, name="tts-answer")
    watcher = Watcher(app, sub, settings)
    watcher.start()
    app._tts_watcher = watcher
    return watcher


def problems(app: Argus) -> list[str]:
    """Voice complaints for the complaints strip (`Argus.problems`): it is on but there is no
    voice — why.
    """
    watcher: Watcher | None = getattr(app, "_tts_watcher", None)
    text = watcher.problem() if watcher is not None else ""
    return [text] if text else []


def routes(app: Argus):
    """`GET /speech/{clip_id}.mp3` returns the bytes of a clip, or 404 if it is not there yet
    or no longer there.

    `clip_id` is `"{t}-{idx}"`: the same reply timestamp (`answer.meta["t"]`, as with
    `/shot/{id}.jpg` in `ui/server.py`) plus the number of the sentence chunk.

    `POST /tts/preview` speaks a trial phrase with a character, bypassing the `Watcher`.
    People listen to a cloned voice before they turn on voice-over for replies, to decide
    whether it is any good, not only after `channel.tts.enabled = true`.
    """

    # The preview state belongs to the app, not to a request: the lock and the time of the
    # last preview (monotonic).
    preview_lock = asyncio.Lock()
    last_preview = [-PREVIEW_GAP_S]

    def add(web) -> None:
        @web.get("/speech/{clip_id}.mp3")
        async def speech_clip(clip_id: str):
            from fastapi import Response

            watcher: Watcher | None = getattr(app, "_tts_watcher", None)
            audio = watcher.clips.get(clip_id) if watcher is not None else None
            if audio is None:
                return Response(status_code=404)
            return Response(audio, media_type="audio/mpeg")

        @web.post("/tts/preview")
        async def tts_preview(payload: dict | None = None):
            from fastapi import HTTPException, Response

            payload = payload or {}
            settings = tts_read(app.cfg)
            character = str(payload.get("character") or settings.active_character)
            if not character:
                raise HTTPException(400, "no voice is selected")
            profile = settings.characters.get(character)
            if profile is None or not profile.voice_id:
                raise HTTPException(404, f"voice “{character}” wasn't found")

            text = speakable(str(payload.get("text") or PREVIEW_TEXT), settings.max_chars)
            # One preview at a time and no more often than `PREVIEW_GAP_S`: synthesis is paid, and
            # the route is open to any tab on this computer. We do not require voice-over to be on:
            # people listen to the voice before they turn it on.
            if preview_lock.locked() or time.monotonic() - last_preview[0] < PREVIEW_GAP_S:
                raise HTTPException(429, PREVIEW_BUSY)
            async with preview_lock:
                last_preview[0] = time.monotonic()
                try:
                    if settings.relay:
                        account = getattr(app, "_account", None)
                        if account is None or not account.signed_in:
                            raise HTTPException(400, "you need to sign in to use voice replies")
                        audio = await client.synthesize_via_relay(text, profile, settings, account)
                    else:
                        api_key = os.environ.get(settings.api_key_env, "").strip()
                        if not api_key:
                            log.warning("speech: empty: %s — nothing to synthesize with",
                                        settings.api_key_env)
                            raise HTTPException(400, "voice replies aren't set up here")
                        audio = await client.synthesize(text, profile, settings, api_key)
                except HTTPException:
                    raise
                except Exception as e:  # noqa: BLE001 — the voice-over service refused, not our code
                    log.warning("speech failed: %s: %s", type(e).__name__, e)
                    raise HTTPException(
                        502, f"voice reply failed: {friendly.explain(e)}"
                    ) from e
            return Response(audio, media_type="audio/mpeg")

    return add
