"""Speech recognition through GPT-Live-Transcribe, a specialized model.

It is not a conversational model stretched onto the transcription task (as the ordinary
`gpt-realtime-mini` was in the transcriber role), and not a conversational session with
transcription on the side (as `RealtimeSTTConfig` became). It is a separate Realtime API
session type, `session.type: "transcription"`, built specifically for recognition, with
no channel for a reply at all. The question "what if the model decides to answer on the
merits instead of transcribing" is not handled here by a heuristic but removed
structurally: `response.create` does not exist as a concept in this session type, and
there is nothing to call it from.

The model accumulates the conversation context by itself. According to the OpenAI
documentation, each following transcription inside a session takes into account the
previous utterances of the same session, which is the same purpose for which a live
session is kept in the other engines of this package. Plus there are three manual levers
that can be updated on the fly with a repeated `session.update`: `prompt` (what the
conversation is about), `terms` (specific words) and `language`.

The protocol is built from the OpenAI documentation (July-August 2026) and has not been
checked against the live API. There is no independent measurement of speed and accuracy
for this model anywhere yet, and the model itself came out recently. On any schema
mismatch the server answers with an `error` containing the full refusal text, and it is
printed in full: on the first live run this is the only thing that will show where the
documentation differs from the facts.

The design is the same as `RealtimeOpenAI` in this package: `STTProvider` is synchronous
and works one item at a time, Realtime is a persistent WebSocket, and the bridge between
them is a separate thread with its own event loop. The code shared with that model
(resampling to PCM16, parsing transcription events) is duplicated here instead of
imported from there: that file is itself changing now, and pulling internals from it
would mean catching someone else's edit halfway.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
from concurrent.futures import Future, InvalidStateError
from typing import TYPE_CHECKING, Any

import numpy as np

from argus.core.events import SAMPLE_RATE, SpeechEvent, Utterance

if TYPE_CHECKING:
    from argus.core.config import GPTLiveTranscribeConfig

log = logging.getLogger(__name__)


class GPTLiveTranscribeError(RuntimeError):
    """The session could not be brought up: key, network, or a refusal from the server."""


# Failures after which the session will not come back to life by itself. `session_expired`
# is the sixty-minute ceiling common to all types of Realtime sessions; the server closes
# the session and rejects everything after that. The other two are about the key and the
# limits: recreating the session will not help, but marking it dead is still more correct
# than silently getting a refusal on every utterance until a restart.
SESSION_IS_OVER = frozenset({"session_expired", "invalid_api_key", "insufficient_quota"})

# A marker in the `event_id` of our deletions from the history: a refusal of such a
# deletion is not about an utterance.
TRIM_EVENT = "trim-"


def _to_pcm16(samples: np.ndarray, src_rate: int, dst_rate: int) -> bytes:
    """Convert mono int16 to the required rate and return it as raw bytes.

    Realtime expects 24 kHz, while everything inside our code lives at 16 kHz: resampling is
    mandatory, otherwise the model would hear speech sped up one and a half times.
    """
    if src_rate == dst_rate:
        return samples.astype(np.int16).tobytes()

    import soxr

    x = samples.astype(np.float32) / 32768.0
    y = soxr.resample(x, src_rate, dst_rate)
    return np.clip(y * 32768.0, -32768, 32767).astype(np.int16).tobytes()


def _text_from_event(msg: dict[str, Any]) -> str:
    """Dig a piece of text out of a streaming event.

    Event names in Realtime have changed more than once over its lifetime. Parsing them from
    a list means breaking on every rename, so we take any event that has a string field
    `delta`: the shape is more reliable than the name.
    """
    kind = msg.get("type", "")
    if not kind.endswith(".delta"):
        return ""
    if "audio" in kind and "transcript" not in kind:
        return ""      # this is audio, not text
    delta = msg.get("delta")
    return delta if isinstance(delta, str) else ""


class GPTLiveTranscribe:
    """A recognizer on top of a GPT-Live-Transcribe transcription session."""

    name = "gpt-live-transcribe"

    # Its own instance per channel, for the same reason as the other session-based engines:
    # context accumulates inside the session, and a session shared by two channels would mix
    # both sides of the conversation and make them wait for each other.
    per_channel = True

    def __init__(self, cfg: GPTLiveTranscribeConfig, label: str = "") -> None:
        self.cfg = cfg
        self.label = label or f"{self.name}/{cfg.model}"

        # Why the session is considered dead. Empty means alive.
        self._dead = ""
        self.revivals = 0

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._closing = False

        # Identifiers of utterances in the session, used to trim the history.
        self._items: list[str] = []
        self._trims = 0                              # how many deletions have been sent (for `event_id`)
        self._turn_item: str | None = None           # the item of the utterance that is waiting for text right now

        self._task: asyncio.Task | None = None

        # Utterances go strictly one at a time, so a single slot is enough.
        self._pending: Future[str] | None = None
        self._buffer: list[str] = []

        # Whether the audio of the last utterance went to the network: only what was sent is paid
        # for (`SpeechPipeline._handle`).
        self.audio_sent = False

        # Fresh prompt/terms: what will go into session.update on the next `warmup`, and what can
        # be changed on the fly through `update_context`. We keep them separate from `cfg`: the
        # config holds the starting values, while the conversation may produce new terms that did
        # not exist at startup.
        self._prompt = cfg.prompt
        self._terms = cfg.terms

        self.calls = 0
        self.failures = 0

    # -- lifecycle ---------------------------------------------------------

    def warmup(self) -> None:
        """Bring up the session in advance.

        Without this, the very first utterance would wait for the connection to be established,
        and in a latency measurement the first result would be an outlier by a second or a second
        and a half.
        """
        if self._thread is not None:
            return

        key = os.environ.get(self.cfg.api_key_env, "").strip()
        if not key:
            raise GPTLiveTranscribeError(
                f"no key: the environment variable {self.cfg.api_key_env} is empty."
            )

        self._thread = threading.Thread(
            target=self._run_loop, args=(key,), name="gpt-live-transcribe", daemon=True
        )
        self._thread.start()

        if not self._ready.wait(timeout=self.cfg.timeout_s):
            raise GPTLiveTranscribeError(f"the session did not come up within {self.cfg.timeout_s:.0f} s")
        if self._error:
            raise GPTLiveTranscribeError(self._error)

    def reconnect(self, reason: str) -> bool:
        """Close the session and bring it up again with the current settings.

        Session parameters (for example, the language) cannot be switched on the fly, only by
        recreating the connection. If the session has never been brought up, there is no need to
        call this: the next connection will take the fresh config anyway.

        The accumulated context is lost in the process: it lives in the session and cannot be
        brought back.
        """
        if self._thread is None:
            return True
        self.revivals += 1
        log.warning("gpt-live-transcribe: restarting the session (%s)", reason)
        self.close()
        # `close()` raises this flag so that the old session leaves quietly. If it were not put
        # back down, the new session would read the server's first event, see "closing" and exit,
        # together with the loop into which all utterances are then written ("Event loop is
        # closed").
        self._closing = False
        self._dead = ""
        self._error = ""
        self._ready.clear()
        try:
            self.warmup()
        except GPTLiveTranscribeError as e:
            self._dead = str(e)
            log.warning("gpt-live-transcribe: session failed to start: %s", e)
            return False
        return True

    def _revive(self) -> bool:
        """Bring up the session again after the previous one died.

        It dies predictably: a Realtime session of any type has a ceiling of sixty minutes, after
        which the server answers `session_expired` to everything. For a channel where the
        conversation goes on for an hour or an hour and a half, this is not an exception but a
        routine event in the middle of the session.
        """
        return self.reconnect(self._dead or "connection lost")

    def close(self) -> None:
        """Close the session, quickly and completely.

        Closing a WebSocket is a handshake: we send a frame and wait for the answer. If the other
        side does not answer (and at shutdown it most often no longer answers), the wait drags on
        for seconds. Pipeline shutdown must not wait that long, so after a short attempt to close
        politely the loop is aborted forcibly.
        """
        self._closing = True
        thread, loop, task = self._thread, self._loop, self._task
        self._thread = None
        if thread is None:
            return

        if loop is not None and not loop.is_closed() and task is not None:
            loop.call_soon_threadsafe(task.cancel)
            thread.join(timeout=2.0)

            if thread.is_alive():
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=1.0)
        else:
            thread.join(timeout=1.0)

    # -- recognition -------------------------------------------------------

    def transcribe(self, utterance: Utterance) -> SpeechEvent | None:
        return self._speak(utterance)

    def _speak(self, utterance: Utterance, retried: bool = False) -> SpeechEvent | None:
        """Hand the utterance to the model and wait for the transcription.

        We do not raise errors upward: this engine can be the primary or the shadow one (for
        comparison), and in both roles its failure must not bring the pipeline down. An empty
        answer means a switch to the backup or just a lost comparison point, not a crash of the
        conversation.
        """
        if not retried:
            self.audio_sent = False         # we do not reset it on the second attempt: the audio may have gone out on the first

        if self._thread is None:
            try:
                self.warmup()
            except GPTLiveTranscribeError as e:
                log.warning("gpt-live-transcribe: %s", e)
                self.failures += 1
                return None
        elif self._dead and not self._revive():
            self.failures += 1
            return None

        started = time.perf_counter()
        try:
            audio = _to_pcm16(utterance.samples, SAMPLE_RATE, self.cfg.target_rate)
            future: Future[str] = Future()
            self._pending = future
            self._buffer = []
            self._turn_item = None

            assert self._loop is not None
            sending = asyncio.run_coroutine_threadsafe(self._send_turn(audio), self._loop)
            # Otherwise a send exception would vanish, and the utterance would hang for the whole
            # `timeout_s`.
            sending.add_done_callback(lambda done, mine=future: self._on_sent(done, mine))
            text = future.result(timeout=self.cfg.timeout_s)
        except Exception as e:                       # noqa: BLE001
            # One attempt to bring up the session and retry, but exactly one. The utterance waits for
            # its transcription in a live conversation: a second round of fifteen seconds each would
            # turn the loss of one phrase into the loss of a minute.
            if self._dead and not retried and self._revive():
                return self._speak(utterance, retried=True)
            log.warning("gpt-live-transcribe: utterance lost: %s", e)
            self.failures += 1
            return None
        finally:
            self._pending = None

        text = text.strip()
        if not text:
            return None

        self.calls += 1
        return SpeechEvent(
            t=utterance.t,
            t_end=utterance.t_end,
            source=utterance.source,
            text=text,
            engine=self.label,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    # -- context on the fly ------------------------------------------------

    def update_context(self, prompt: str | None = None, terms: str | None = None) -> None:
        """Update the hint during the conversation without reopening the session.

        The session accumulates context automatically, which is its basic property. This method
        is about something else: explicitly slipping in what we already know without
        accumulation, such as the topic of the conversation or fresh jargon noticed in the
        timeline over the last few minutes. It stores the value and sends `session.update` if
        the session is alive right now; if not, the value will simply go into the next `warmup`.
        """
        if prompt is not None:
            self._prompt = prompt
        if terms is not None:
            self._terms = terms
        if self._ws is None or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._ws.send(json.dumps(self._session_config())), self._loop
        )

    # -- network part, lives in its own thread ------------------------------

    def _run_loop(self, key: str) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._task = loop.create_task(self._session(key))
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass                                     # normal close
        except Exception as e:                       # noqa: BLE001
            if not self._closing:
                self._error = str(e)
                self._dead = str(e)
                log.warning("gpt-live-transcribe: session dropped: %s", e)
        finally:
            self._ready.set()
            if self._loop is loop:
                # The session ended: the utterance waiting in it will never get its text, so we say so
                # right away and not after `timeout_s`. The loop check is there so that the end of an old
                # session does not bring down an utterance that has already gone into the new one.
                self._finish(fail="session closed")
            self._drain(loop)
            loop.close()

    @staticmethod
    def _drain(loop: asyncio.AbstractEventLoop) -> None:
        """Let unfinished tasks close before the loop is closed.

        `websockets` keeps its own service tasks: keepalive and connection closing. Closing the
        loop without waiting for them means dumping a wall of "Task was destroyed but it is
        pending" to the console on every recognition stop. It is not an error, but it looks like
        a breakdown.
        """
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        if not pending:
            return
        for t in pending:
            t.cancel()
        try:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception as e:                       # noqa: BLE001
            log.debug("gpt-live-transcribe: leftover tasks were not cleaned up: %s", e)

    async def _session(self, key: str) -> None:
        import websockets

        headers = {"Authorization": f"Bearer {key}"}

        # A new session starts from a clean slate: it does not know the item numbers of the old
        # one, and a server refusal to delete a foreign number could break a live utterance.
        self._items = []
        self._buffer = []
        self._turn_item = None

        async with websockets.connect(self.cfg.url, additional_headers=headers) as ws:
            self._ws = ws
            await ws.send(json.dumps(self._session_config()))
            self._ready.set()
            log.info("gpt-live-transcribe: session open, model %s", self.cfg.model)

            async for raw in ws:
                if self._closing:
                    break
                try:
                    self._on_message(json.loads(raw))
                except Exception as e:               # noqa: BLE001
                    log.debug("gpt-live-transcribe: event not parsed: %s", e)

            # We get here also when the server closed the connection itself. Without a mark, the
            # session would be considered alive, the next utterance would go into a closed socket, and
            # there would be nobody to bring it up again.
            if not self._closing and not self._dead:
                self._dead = "connection closed"
                log.warning("gpt-live-transcribe: server closed the connection")

    def _session_config(self) -> dict[str, Any]:
        """Transcription session setup.

        `session.type: "transcription"` is not `"realtime"`: it is a separate session type made
        specifically for this model, with no conversational channel inside it.

        `delay` gets into the payload only if it is set explicitly: this is one of the places not
        checked against the live API, and an extra field in the request is an extra chance to hit
        a schema mismatch where the server's default could have been used instead.
        """
        transcription: dict[str, Any] = {"model": self.cfg.model}
        if self.cfg.language:
            transcription["languages"] = [self.cfg.language]
        if self._prompt.strip():
            transcription["prompt"] = self._prompt
        if self._terms.strip():
            transcription["keywords"] = [t.strip() for t in self._terms.split(",") if t.strip()]

        audio_input: dict[str, Any] = {
            "format": {"type": "audio/pcm", "rate": self.cfg.target_rate},
            "turn_detection": None,
            "transcription": transcription,
        }
        if self.cfg.delay:
            audio_input["delay"] = self.cfg.delay

        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {"input": audio_input},
            },
        }

    async def _send_turn(self, audio: bytes) -> None:
        """Send the audio and ask for it to be committed.

        `response.create` does not exist as a concept here: this is a transcription session
        type, and there is nothing to answer with. The commit by itself starts the transcription
        of the input.
        """
        ws = self._ws
        if ws is None:
            raise GPTLiveTranscribeError("session closed")

        await ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(audio).decode("ascii"),
        }))
        self.audio_sent = True              # the audio has gone out, and from this moment it is paid
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    def _on_sent(self, sending: Future, mine: Future[str]) -> None:
        """Sending of an utterance has ended. If it failed, there will be no answer, so there is no
        point waiting for it.
        """
        error: BaseException | None
        if sending.cancelled():
            error = GPTLiveTranscribeError("sending cancelled")
        else:
            error = sending.exception()
        if error is None:
            return
        if not self._closing and not self._dead:
            # Writing to the socket failed, so the session is broken; the mark is needed so that the
            # retry brings it up again.
            self._dead = f"sending failed: {type(error).__name__}"
        self._finish(fail=str(error) or type(error).__name__, only=mine)

    def _on_message(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type", "")

        if kind == "error":
            # We print the server's refusal in full: most often it is a session schema mismatch, and
            # the error text is the only thing that will show what exactly it expected. Swallowing it
            # silently here would cost an hour of debugging, especially on a protocol built from
            # documentation and not checked against the facts.
            detail = msg.get("error", msg)
            if self._is_trim_refusal(detail):
                # A refusal to delete from the history (the item is already gone) does not get in the
                # utterance's way.
                log.debug("gpt-live-transcribe: history trimming failed: %s", detail)
                return
            log.warning(
                "gpt-live-transcribe: server rejected: %s", json.dumps(detail, ensure_ascii=False)
            )
            code = detail.get("code") if isinstance(detail, dict) else ""
            if code in SESSION_IS_OVER:
                self._dead = str(code)
            self._finish(fail=str(detail))
            return

        # A late event of a previous utterance (it timed out) is not ours.
        if self._is_stale(msg.get("item_id")):
            return

        chunk = _text_from_event(msg)
        if chunk:
            self._buffer.append(chunk)
            return

        if "input_audio_transcription" in kind:
            if kind.endswith(".completed"):
                self._finish(fallback=str(msg.get("transcript") or ""))
            elif kind.endswith(".failed"):
                self._finish(fail=str(msg.get("error", msg)))
            return

        if kind.startswith("conversation.item."):
            item = msg.get("item") or {}
            iid = item.get("id") if isinstance(item, dict) else None
            if iid and iid not in self._items:
                self._items.append(iid)
                if self._pending is not None:
                    self._turn_item = iid           # a new item while an utterance is waiting is its audio
                self._trim_history()
            return

    def _is_stale(self, item_id: Any) -> bool:
        """An event about an item of a previous utterance. Only about one that is known for certain to
        be previous: an unfamiliar number is let through, so that a foreign event scheme does not turn
        into the loss of all utterances."""
        return (
            isinstance(item_id, str)
            and item_id in self._items
            and item_id != self._turn_item
        )

    @staticmethod
    def _is_trim_refusal(detail: Any) -> bool:
        """A refusal of our deletion from the history: by the mark in `event_id` or by the code "no such
        item".
        """
        if not isinstance(detail, dict):
            return False
        event_id = detail.get("event_id")
        if isinstance(event_id, str) and event_id.startswith(TRIM_EVENT):
            return True
        return detail.get("code") == "item_not_found"

    def _finish(self, fallback: str = "", fail: str | None = None,
                only: Future[str] | None = None) -> None:
        """Close the waiting utterance with text or with a refusal. `only` means close it only if it is
        this one that is waiting: a late message about sending must not touch the next one."""
        future = self._pending
        if only is not None and future is not only:
            return
        self._pending = None
        if future is None or future.done():
            return
        try:
            if fail is not None:
                future.set_exception(GPTLiveTranscribeError(fail))
            else:
                future.set_result("".join(self._buffer).strip() or fallback)
        except InvalidStateError:
            pass                                     # closed at the same time from two threads

    def _trim_history(self) -> None:
        """Throw out of the session the utterances outside the window.

        Context is what the session stays open for, but the history is re-billed on every turn:
        without trimming, the bill grows quadratically with the length of the conversation. One
        item goes per turn (the transcription adds nothing on its own behalf, it only marks an
        already existing item with audio), so `keep_turns` here equals the number of utterances
        directly.
        """
        limit = self.cfg.keep_turns
        if limit <= 0 or len(self._items) <= limit or self._ws is None:
            return

        stale, self._items = self._items[:-limit], self._items[-limit:]
        for item_id in stale:
            self._trims += 1
            asyncio.run_coroutine_threadsafe(
                self._ws.send(json.dumps({
                    "type": "conversation.item.delete", "item_id": item_id,
                    "event_id": f"{TRIM_EVENT}{self._trims}",
                })),
                self._loop,                          # type: ignore[arg-type]
            )

    def stats(self) -> dict[str, int]:
        return {
            "recognized": self.calls,
            "lost": self.failures,
        }
