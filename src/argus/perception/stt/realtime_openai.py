"""Speech recognition by an audio-native model, in a live session.

This is **not a transcriber in the old sense**, and the difference is one of principle,
not of quality. An ordinary transcriber (Whisper, Deepgram) turns sound into text knowing
only a language prior. It has to put the name "Roshan" on paper in Russian letters, and
the prior has no such word, so it comes out as "Roshen" (a misspelling). The only lever
against this is a word list, but the list has to be known in advance and maintained by
hand, and by definition it does not catch new slang. The ceiling here is structural, not
a matter of model size.

Here the transcription is done by a model that has the accumulated conversation in front
of it ("Roshan" is recognized not from a list but because the last five minutes were
about Dota), but **this model cannot answer at all**, because it has no channel for it.
Before, a conversational model sat in its place, told by a prompt to stay silent and
only write things down. It occasionally disobeyed and answered on the merits instead of
transcribing (this was caught by a heuristic on speech rate and kept a whole layer of
protection around it). Now we use the input transcription of Realtime itself,
`session.audio.input.transcription`. It does not generate a model reply, it only reads
what came in. We simply never call `response.create`, and there is physically nothing to
answer with: the slip into dialogue disappeared not through a heuristic but because there
is no dialogue here anymore.

The session is still the same `realtime` session as before (not a separate
`transcription` intent). It is kept open between utterances for the same reason as
before: context accumulates in it by itself. Reconnecting resets what was accumulated,
and the first utterances of the new session are recognized at the level of an ordinary
transcriber.

Design
------
The `STTProvider` interface is synchronous and works one item at a time,
`transcribe(utterance)`, while Realtime is a persistent two-way WebSocket. The bridge is
a separate thread with its own event loop: `transcribe` puts a job in and blocks on the
answer, and all the network work lives in that thread. This way the provider plugs into
the pipeline on equal terms, without rewriting it for itself.

The cutting into utterances is done by our VAD, not the server's: a comparison with
another engine makes sense only on **the same** pieces of audio. So `turn_detection` is
turned off on the server, and we set the utterance boundaries.
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

from argus.core import friendly
from argus.core.events import SAMPLE_RATE, SpeechEvent, Utterance

if TYPE_CHECKING:
    from argus.account.client import Account
    from argus.core.config import RealtimeSTTConfig

log = logging.getLogger(__name__)


class RealtimeSTTError(RuntimeError):
    """The session could not be brought up: key, network, or a refusal from the server."""


class RelayRefused(RealtimeSTTError):
    """The account server refused by itself: not signed in, the login expired, no money.

    Unlike other engine errors, the text here is written for people ("your balance is
    empty — top up…"), and it must be shown as it is, not with
    the generic phrase "no access".
    """

    for_people = True


class NotSignedIn(RelayRefused):
    """There is no account sign-in. At launch we do not complain about it: the hint "you need to sign
    in — it's in settings" (`account_hint`) is already on the page
    and with a link, and a complaint would stand on top of it."""

    quiet = True


# Failures after which the session will not come back to life by itself.
#
# `session_expired` is the sixty-minute ceiling: the server closes the session and rejects
# everything after that. The other two are about the key and the limits; there too,
# recreating the session will not help, but marking it dead is still more correct than
# silently getting a refusal on every utterance until a restart.
SESSION_IS_OVER = frozenset({"session_expired", "invalid_api_key", "insufficient_quota"})

# A marker in the `event_id` of our deletions from the history. A refusal of such a
# deletion ("no such item") is not about an utterance or the session: by the marker it is
# filtered out and cannot break a waiting transcription.
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

    Event names in Realtime have changed more than once over its lifetime, and most likely
    the same fate awaits `conversation.item.input_audio_transcription`. Parsing them from a
    list means breaking on every rename, so we take any event that has a string field
    `delta`: the shape is more reliable than the name.
    """
    kind = msg.get("type", "")
    if not kind.endswith(".delta"):
        return ""
    if "audio" in kind and "transcript" not in kind:
        return ""      # this is audio, not text
    delta = msg.get("delta")
    return delta if isinstance(delta, str) else ""


class RealtimeOpenAI:
    """A recognizer on top of the input transcription of an OpenAI Realtime session."""

    name = "realtime-openai"

    # Its own instance per channel. The point of a session is that the context of the
    # conversation accumulates in it; a shared one would mix both sides in it and bring back the
    # "who is speaking" problem that separate streams solved. Besides, a session serves
    # utterances one at a time: a shared one would make the other party's question wait until a
    # long utterance from the user is finished.
    per_channel = True

    def __init__(self, cfg: RealtimeSTTConfig, label: str = "", account: Account | None = None) -> None:
        self.cfg = cfg
        self._label = label

        # If there is an account, the audio goes through the server (`server/relay_stt.py`): there
        # is no key on this computer and there must not be one, and the server counts the money.
        # If not, it goes straight to the cloud with the key from the environment (own build,
        # development, replay).
        self.account = account

        # Why the last attempt failed, as a phrase for people; empty means the reason is internal
        # or everything is fine. The pipeline shows it among the page's complaints.
        self.last_error = ""

        # The tier with which the current session was opened: the usage meter takes the rate from
        # it. It cannot be read from the config, because the user may already have flipped the
        # switch, while the session listens with the previous model until it is reopened. The
        # first value is for an utterance before the first connection.
        self._tier: str = cfg.tier

        # Why the session is considered dead. Empty means alive.
        #
        # This engine is the only one: there is no backup behind it any more, and its death means
        # silence instead of a conversation. And it dies predictably: a realtime session has a hard
        # ceiling of sixty minutes.
        self._dead = ""
        self.revivals = 0

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._exc: BaseException | None = None       # the same thing, but through the exception itself
        self._closing = False

        # Identifiers of utterances in the session, used to trim the history. It is also described
        # here in text: before, there were two items per turn (the user's audio and the model's
        # reply), now there is one: the input transcription generates nothing, it only marks an
        # existing item.
        self._items: list[str] = []
        self._trims = 0                              # how many deletions have been sent (for `event_id`)

        # The item of the utterance that is waiting for transcription right now. It filters out the
        # late events of earlier utterances; otherwise someone else's `completed` would close the
        # current one.
        self._turn_item: str | None = None

        # The session task: cancelling it is the normal way to close the connection.
        self._task: asyncio.Task | None = None

        # The answer that is being assembled right now. Utterances go strictly one at a time, so a
        # single slot is enough.
        self._pending: Future[str] | None = None
        self._buffer: list[str] = []

        self.calls = 0
        self.failures = 0

        # Whether the audio of the last utterance went to the network. Only what went out is paid
        # for: the pipeline charges seconds by this flag (`SpeechPipeline._handle`), not by the
        # mere fact of an utterance.
        self.audio_sent = False

    @property
    def label(self) -> str:
        """The engine name in events and the log. With the model that is listening now: after a click on
        the switch the previous label would be lying."""
        return self._label or f"{self.name}/{self.cfg.active_model}"

    @property
    def tier(self) -> str:
        """The price of the open session ("normal" or "high"), for expense accounting
        (`SpeechPipeline._handle` -> `Meter.add`)."""
        return self._tier

    @property
    def metered_by_server(self) -> bool:
        """The usage is counted by the server (the relay), not by the app: `Meter` is not needed
        here.

        Otherwise the seconds would be charged twice: by the relay for the audio that went to the
        cloud, and once more by the app's report (`POST /meter/stt`).
        """
        return self.account is not None

    # -- lifecycle ---------------------------------------------------------

    def warmup(self) -> None:
        """Bring up the session in advance.

        Without this, the very first utterance would wait for the connection to be established,
        and in a latency measurement the first result would be an outlier by a second or a second
        and a half.
        """
        if self._thread is not None:
            return

        url, headers = self._connection()

        self._exc = None
        self._thread = threading.Thread(
            target=self._run_loop, args=(url, headers), name="realtime-stt", daemon=True
        )
        self._thread.start()

        if not self._ready.wait(timeout=self.cfg.timeout_s):
            raise RealtimeSTTError(f"the session did not come up within {self.cfg.timeout_s:.0f} s")
        if self._error:
            # We keep the type: a refusal from the account server (`RelayRefused`) is written for
            # people, and as a string it would turn into an ordinary error with a generic phrase.
            raise self._exc if isinstance(self._exc, RealtimeSTTError) else RealtimeSTTError(self._error)

    def _connection(self) -> tuple[str, dict[str, str]]:
        """Where and with what to connect: to the account server by pass, or straight to the cloud by
        key.
        """
        if self.account is not None:
            return self._relay_connection(self.account)

        key = os.environ.get(self.cfg.api_key_env, "").strip()
        if not key:
            raise RealtimeSTTError(
                f"no key: the environment variable {self.cfg.api_key_env} is empty. "
                f"Engine comparison will not start without it."
            )
        return f"{self.cfg.url}?model={self.cfg.model}", {"Authorization": f"Bearer {key}"}

    @staticmethod
    def _relay_connection(account: Account) -> tuple[str, dict[str, str]]:
        from argus.account.client import AccountError

        if not account.signed_in:
            raise NotSignedIn("you need to sign in — it's in settings")
        try:
            token = account.access_token()
        except AccountError as e:
            # "session expired — sign in again", "you need to sign in": the server's text for
            # people.
            raise RelayRefused(str(e)) from None
        except Exception as e:                       # noqa: BLE001
            # The pass exchange goes over the network, and a raw httpx error would come out of here
            # unwrapped.
            raise RealtimeSTTError(f"no connection to the server: {type(e).__name__}") from None
        return account.ws_url("/relay/stt"), {"Authorization": f"Bearer {token}"}

    def reconnect(self, reason: str) -> bool:
        """Close the session and bring it up again with the current settings.

        Session parameters (for example, the language) cannot be switched on the fly, only by
        recreating the connection. If the session has never been brought up, there is no need to
        call this: the next connection will take the fresh config anyway.

        The accumulated context is lost in the process: it lives in the session and cannot be
        brought back. This is a price, and it is smaller than the alternative: a conversation
        with no recognition at all.
        """
        if self._thread is None:
            return True
        self.revivals += 1
        log.warning("realtime-stt: restarting the session (%s)", reason)
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
        except RealtimeSTTError as e:
            # If it failed, we record that and do not hammer on every utterance: the next attempt will
            # happen by itself when the next utterance comes. Meanwhile the network either comes back
            # or not, and the latter is visible on the dashboard.
            self._dead = str(e)
            self._remember(e)
            log.warning("realtime-stt: session failed to start: %s", e)
            return False
        return True

    def _remember(self, error: BaseException) -> None:
        """Remember the reason for a refusal if it is written for people (`RelayRefused`)."""
        self.last_error = str(error) if getattr(error, "for_people", False) else ""

    def _revive(self) -> bool:
        """Bring up the session again after the previous one died.

        Before, this engine was the second one and could afford to die: the local Whisper picked
        up the utterance. Now it is the only one, and any death of it means silence instead of a
        conversation.

        It dies predictably: a realtime session has a hard ceiling of sixty minutes, after which
        the server answers `session_expired` to everything. For a channel where the conversation
        goes on for an hour or an hour and a half, this is not an exception but a routine event
        in the middle of the session.
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
            # We cancel the session task instead of aborting the loop: the cancellation passes through
            # the `async with` of websockets, and the connection is closed by the rules. Aborting the
            # loop would leave unfinished service tasks behind.
            loop.call_soon_threadsafe(task.cancel)
            thread.join(timeout=2.0)

            if thread.is_alive():
                # Closing politely failed: the socket does not answer. The thread is a daemon and will not
                # stop the process from exiting, but we cannot leave it hanging: on every recognition
                # restart one more of them would pile up.
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=1.0)
        else:
            thread.join(timeout=1.0)

    # -- recognition -------------------------------------------------------

    def transcribe(self, utterance: Utterance) -> SpeechEvent | None:
        return self._speak(utterance)

    def _speak(self, utterance: Utterance, retried: bool = False) -> SpeechEvent | None:
        """Hand the utterance to the model and wait for the transcription.

        We do not raise errors upward in either of the two roles this engine can have. As the
        shadow engine it runs next to the primary one for comparison, and its failure must not
        bring the pipeline down. As the primary one, a local engine runs next to it in just the
        same way, and an empty answer from here means not a lost utterance but a switch to the
        backup.
        """
        if not retried:
            # We do not reset it on the second attempt: the audio may already have gone out on the
            # first one, and it is paid for.
            self.audio_sent = False

        if self._thread is None:
            try:
                self.warmup()
            except RealtimeSTTError as e:
                log.warning("realtime-stt: %s", e)
                self.failures += 1
                self._remember(e)
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
            # Otherwise a send exception (socket closed, no session) would vanish together with this
            # Future, and the utterance would hang for the whole `timeout_s`, and the whole pipeline
            # with it.
            sending.add_done_callback(lambda done, mine=future: self._on_sent(done, mine))
            text = future.result(timeout=self.cfg.timeout_s)
        except Exception as e:                       # noqa: BLE001
            # One attempt to bring up the session and retry, but exactly one. The utterance waits for
            # its transcription in a live conversation: a second round of fifteen seconds each would
            # turn the loss of one phrase into the loss of a minute.
            if self._dead and not retried and self._revive():
                return self._speak(utterance, retried=True)
            log.warning("realtime-stt: utterance lost: %s", e)
            self.failures += 1
            return None
        finally:
            self._pending = None

        text = text.strip()
        if not text:
            return None

        # This path has nothing to answer with: it reads only the input, and the model's reply is
        # never requested here (`response.create` is never called). Before, there was a check here
        # for "does this look like a reply rather than a transcription". It was needed exactly
        # because the engine was a conversational model. Removing this check is not a risk: there
        # is simply nothing left to check.
        self.calls += 1
        self.last_error = ""
        return SpeechEvent(
            t=utterance.t,
            t_end=utterance.t_end,
            source=utterance.source,
            text=text,
            engine=self.label,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    # -- network part, lives in its own thread ------------------------------

    def _run_loop(self, url: str, headers: dict[str, str]) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._task = loop.create_task(self._session(url, headers))
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass                                     # normal close
        except Exception as e:                       # noqa: BLE001
            # On shutdown the session is cut off deliberately, and there is nothing to complain about.
            # Without this check, every normal exit would write an alarm to the log.
            if not self._closing:
                # A network failure (connection refused, no network, reset) is wrapped in our own phrase
                # on purpose: the OS text (on a localized Windows it is localized) tells the user nothing,
                # and the general classifier gets it wrong.
                self._exc = RealtimeSTTError("no connection") if isinstance(e, OSError) else e
                self._error = str(self._exc)
                # It was cut off not by our will, so it has to be brought up again. A network blip, the
                # machine going to sleep, the server closing the connection: all of this is cured by a new
                # session, not by waiting.
                self._dead = str(e)
                log.warning("realtime-stt: session dropped: %s", e)
        finally:
            self._ready.set()
            if self._loop is loop:
                # The session is over (the server closed it, a drop, a cancellation): an utterance that was
                # still waiting for text in it will never get it. We say so right away; otherwise `_speak`
                # would stand for the whole `timeout_s`, and both channels would stand with it. The loop
                # check is there so that the late end of an old session does not bring down an utterance
                # that has already gone into the new one.
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
            # Cleanup after an already closed session: if that fails too, nothing can be done, but
            # there is no reason to crash on exit either.
            log.debug("realtime-stt: leftover tasks were not cleaned up: %s", e)

    async def _session(self, url: str, headers: dict[str, str]) -> None:
        import websockets

        # A new session starts from a clean slate. The new session does not know the item numbers
        # of the old one: the server answers a deletion of a foreign number with a refusal, and the
        # refusal could break a live utterance.
        self._items = []
        self._buffer = []
        self._turn_item = None

        # `close_timeout` is short on purpose. Closing is a handshake: we send a frame and wait
        # until the server closes the connection from its side, and by default we wait up to ten
        # seconds. The server does not answer at once, and in a measurement on a live session every
        # close hit exactly two seconds (`close()` below waits that long and gives up): app
        # shutdown and a session restart on a language change wasted those two seconds. After this
        # we do not need the connection itself: it is already closed from our side, and the server
        # will finish it on its own.
        try:
            async with websockets.connect(
                url, additional_headers=headers, close_timeout=0.3
            ) as ws:
                self._ws = ws
                if self.account is not None:
                    await self._relay_ready(ws)
                await ws.send(json.dumps(self._session_config()))
                self._ready.set()
                log.info(
                    "realtime-stt: session open, listening on input %s", self.cfg.active_model
                )

                async for raw in ws:
                    if self._closing:
                        break
                    try:
                        self._on_message(json.loads(raw))
                    except Exception as e:           # noqa: BLE001
                        log.debug("realtime-stt: event not parsed: %s", e)

                # We get here also when the server closed the connection itself: because of the session
                # ceiling, a long silence, or because the money ran out. Without this mark, the session
                # would be considered alive: the next utterance would go into a closed socket, and there
                # would be nobody to bring it up again.
                if not self._closing and not self._dead:
                    self._dead = "connection closed"
                    log.warning("realtime-stt: server closed the connection")
        except websockets.exceptions.InvalidStatus as e:
            # The handshake was not accepted. Through the account server this is almost always a pass
            # that it did not recognize (the account was deleted, the login went stale).
            if self.account is None:
                raise
            code = e.response.status_code
            if code in (401, 403):
                raise RelayRefused("sign-in not accepted — sign in to your account again") from None
            raise RealtimeSTTError(f"the server answered {code}") from None

    async def _relay_ready(self, ws: Any) -> None:
        """Wait until the account server opens the cloud and says "ready".

        A refusal arrives as an event with a reason (`insufficient_quota` and similar): its text
        is written for people, and it must reach the user as it is, "your balance is
        empty", and not "something went wrong".
        """
        try:
            async with asyncio.timeout(self.cfg.timeout_s):
                while True:
                    msg = json.loads(await ws.recv())
                    kind = msg.get("type")
                    if kind == "relay.ready":
                        return
                    if kind == "error":
                        err = msg.get("error")
                        text = str(err.get("message") or "") if isinstance(err, dict) else ""
                        if friendly.plain(text):
                            raise RelayRefused(text)
                        raise RealtimeSTTError(text or "the server refused")
        except TimeoutError:
            raise RealtimeSTTError("the server did not answer in time") from None
        except Exception as e:                       # noqa: BLE001
            if isinstance(e, RealtimeSTTError):
                raise
            # Closed without giving a reason: treat it as a dropped connection.
            raise RealtimeSTTError(f"the server closed the connection: {type(e).__name__}") from None

    def _session_config(self) -> dict[str, Any]:
        """Session setup.

        The session is still the same `realtime` (not a separate `transcription` intent), since
        the context between utterances accumulates exactly in it. The value is in the
        `transcription` block inside the input audio:

        * `model` is a separate transcription model (`gpt-4o-mini-transcribe` and similar), not a
          conversational one. It has nothing to answer with, because it has no channel for it,
          and the whole class of "the model answered instead of writing down" structurally does
          not exist with it, rather than being filtered out by a heuristic after the fact.
        * `prompt` is a short hint about terms and proper names, if set: the same purpose that
          the word list had before in the transcriber role.
        * `turn_detection: null` in `audio.input`: the utterance boundaries are still cut by our
          VAD, not by the server, because a comparison of engines makes sense only on the same
          pieces of audio.

        The output voice (`output_modalities`) has nothing to do with it at all: we never call
        `response.create`, and the model simply has no output.
        """
        self._tier = self.cfg.tier
        transcription: dict[str, Any] = {"model": self.cfg.active_model}
        if self.cfg.language:
            transcription["language"] = self.cfg.language
        if self.cfg.terms.strip():
            transcription["prompt"] = self.cfg.terms

        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": self.cfg.target_rate},
                        "turn_detection": None,
                        "transcription": transcription,
                    }
                },
            },
        }

    async def _send_turn(self, audio: bytes) -> None:
        """Send the audio and ask for it to be committed, and never ask for a reply.

        `response.create` is never here: that is exactly what distinguishes this path from a
        conversational model in the transcriber role. The commit by itself starts the
        transcription of the input; nobody waits for a reply and nobody can give one.
        """
        ws = self._ws
        if ws is None:
            raise RealtimeSTTError("session closed")

        await ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(audio).decode("ascii"),
        }))
        # The audio has gone out, and from this moment it is paid, whatever happens to the
        # transcription afterwards.
        self.audio_sent = True
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    def _on_sent(self, sending: Future, mine: Future[str]) -> None:
        """Sending of an utterance has ended. If it failed, there will be no answer, so there is no
        point waiting for it.
        """
        error: BaseException | None
        if sending.cancelled():
            error = RealtimeSTTError("sending cancelled")
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
            # silently here would cost an hour of debugging.
            detail = msg.get("error", msg)
            if self._is_trim_refusal(detail):
                # A refusal to delete from the history: the item is already gone (the session is new, or
                # the server trimmed it itself). It has nothing to do with the utterance, and it must not
                # break it.
                log.debug("realtime-stt: history trimming failed: %s", detail)
                return
            log.warning("realtime-stt: server rejected: %s", json.dumps(detail, ensure_ascii=False))

            # Some refusals are about this utterance, some about the whole session, and they are
            # handled differently. `session_expired` arrives at the sixty-minute ceiling, and after it
            # the server will reject everything: until the session is brought up again, there will be
            # no recognition at all.
            code = detail.get("code") if isinstance(detail, dict) else ""
            if code in SESSION_IS_OVER:
                self._dead = str(code)
            # The account server writes the reason for people ("your balance is empty…");
            # the cloud writes technical English, and such text is not shown to the user.
            said = str(detail.get("message") or "") if isinstance(detail, dict) else ""
            if self.account is not None and friendly.plain(said):
                self.last_error = said
            self._finish(fail=str(detail))
            return

        # A late event of a previous utterance (it timed out) is not ours: without filtering, it
        # would close, with someone else's text, the utterance that is waiting now.
        if self._is_stale(msg.get("item_id")):
            return

        chunk = _text_from_event(msg)
        if chunk:
            self._buffer.append(chunk)
            return

        # The input transcription is tied to the same item as the audio itself, not to a separate
        # reply, so we handle its `.completed`/`.failed` before the general `conversation.item.`
        # branch. Otherwise they would be caught by the same path as `.added`/`.done` and would
        # never reach `_finish`.
        if "input_audio_transcription" in kind:
            if kind.endswith(".completed"):
                self._finish(fallback=str(msg.get("transcript") or ""))
            elif kind.endswith(".failed"):
                self._finish(fail=str(msg.get("error", msg)))
            return

        if kind.startswith("conversation.item."):
            # Measured on the live API: the event is called `conversation.item.added`, and `.done`
            # with the same id follows. We take any of the family and filter out the repeat by id.
            # Tying to the exact name has already once resulted in a history trim that did not work.
            item = msg.get("item") or {}
            iid = item.get("id") if isinstance(item, dict) else None
            if iid and iid not in self._items:
                self._items.append(iid)
                if self._pending is not None:
                    # A new item while an utterance is waiting for text is its audio (on commit the server
                    # creates exactly one, and the newest one always belongs to the last utterance). Events of
                    # other items are foreign.
                    self._turn_item = iid
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
        """A refusal of our deletion from the history: by the mark in `event_id`, and via the account
        server by the code "no such item" (it does not forward the mark)."""
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
                future.set_exception(RealtimeSTTError(fail))
            else:
                future.set_result("".join(self._buffer).strip() or fallback)
        except InvalidStateError:
            # Closed at the same time from two threads: the first one won, and the second has nothing
            # to add.
            pass

    def _trim_history(self) -> None:
        """Throw out of the session the utterances outside the window.

        Context is what the provider exists for, but the history is re-billed on every turn:
        without trimming, the bill grows quadratically with the length of the conversation.

        We count items, not turns, and now these are the same thing: the input transcription adds
        nothing to the conversation on its own behalf, it only marks an already existing item
        with audio. Before, two items went per turn (the audio and the model's reply), so
        `keep_turns=20` kept the last ten utterances; now it keeps twenty.
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
