"""Speech recognition relay: the audio goes to the server, the key stays on the server.

Previously recognition ran with a key on the person's computer, straight to the cloud,
bypassing us (the server learned about it only from the client's report, `/meter/stt`).
Such a build cannot be distributed: the key was in it, and recognition was free for anyone
who can read `.env`. And a build without a key did not hear at all: a friend downloaded
the app, pressed the recognition button, and nothing happened.

Now the app opens a WebSocket here (`/relay/stt`), and the server opens a second one to
the cloud and passes audio and text between them. The client speaks the same protocol as
before with the cloud directly, so its code has barely changed. But between the two
sockets stands everything that makes the relay a lock rather than a pipe:

* **Only what is needed.** Of the client's messages, only audio, its commit, the session
  setup, and deletion of an old item are let out, and nothing else. The session setup is
  not forwarded but rebuilt from two allowed fields (language and hint) and a model from
  `LISTENERS`: the model name comes from the client, and without a list anyone would
  demand the most expensive one. `response.create` and anything else that would make the
  cloud answer at our expense does not get through.
* **Money is here.** Seconds of audio are counted by what really went to the cloud and are
  charged to the balance every `FLUSH_S` seconds and on close. As with answers
  (`relay.py`), money is not checked but held (`entitlements.hold`): before a piece of
  audio, a window of `HOLD_S` seconds is taken for it with one conditional `UPDATE`, so
  however many sessions and calls run at once, they cannot split the same remainder. The
  window is closed with the real usage (`settle_many`), and the unused part is returned.
  At any end of a session (a break, an error, a cancel) everything that did not go into
  usage is returned. If the process dies, `recover_holds` returns the held amount at
  startup. An open session does not live on its own: every `CHECK_S` seconds the account
  is checked again, and a closed one, or one left without money, closes the socket
  politely, with an event carrying the reason, like a refusal at opening. The client no
  longer reports (`/meter/stt` is not needed for it).
* **Only text goes out.** Of the cloud's events, the client gets the transcription, the
  item ids (for trimming the history), and depersonalized refusals. No session
  descriptions, no model names, no provider error text: it can contain a piece of a key.
* **Concurrency.** No more than `MAX_SESSIONS` sessions per account (the app needs two,
  one session per channel) and `MAX_SESSIONS_TOTAL` for the whole server, and a session in
  which the client is silent for longer than `IDLE_S` is closed.
* **The history does not grow without limit.** The app trims the session itself
  (`keep_turns`), but its honesty cannot be relied on here: the cloud rebills the history
  on every turn, and a client that does not trim would push our cost above the price it
  pays. So the server keeps its own count of the session's items and deletes the oldest
  beyond `MAX_ITEMS`.

Audio and text are not written anywhere here, neither to disk nor to the log: the relay
forwards them and counts seconds. The privacy policy promises exactly this.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket

from argus.server import entitlements, pricing

log = logging.getLogger(__name__)

# Where we forward to. Changed in tests (a fake server).
UPSTREAM_URL = "wss://api.openai.com/v1/realtime"
# The model of the session itself is a conversational one, and the server chooses it, not
# the client. It can be changed without deploying code through the variable
# `ARGUS_STT_REALTIME_MODEL` in the server's `.env`.
REALTIME_MODEL = "gpt-realtime-mini"

# Models the client may set to listen to the input, and the price of each
# (`pricing.STT_TIERS`). What is not here, the relay does not sell.
LISTENERS = {
    "gpt-4o-mini-transcribe": "normal",
    "gpt-4o-transcribe": "high",
}
DEFAULT_LISTENER = "gpt-4o-mini-transcribe"

SAMPLE_RATE = 24_000                      # Hz, 16 bit, mono: the session format
BYTES_PER_SECOND = SAMPLE_RATE * 2

# A piece of audio larger than this is not accepted: an honest client sends one utterance at
# a time, and the VAD cuts an utterance at 30 seconds (`vad.max_utterance_s` <= 30). Allowing
# more than needed is not safe: the whole message sits in the server's memory, and a dozen
# open sessions with huge frames took the container out of memory (audit 2026-10-16).
# uvicorn holds the same limits (`--ws-max-size` in the Dockerfile).
MAX_APPEND_S = 35.0
MAX_APPEND_B64 = int(MAX_APPEND_S * BYTES_PER_SECOND * 4 / 3) + 8

MAX_SESSIONS = 4          # per account
MAX_SESSIONS_TOTAL = 200  # for the whole server: each session has its own socket to the cloud and its own memory
MAX_ITEMS = 200           # history items per session; also the upper limit of `keep_turns` in the app
IDLE_S = 30 * 60          # how long the client may stay silent without being closed
FLUSH_S = 10.0            # after how many seconds of audio to charge
OPEN_TIMEOUT_S = 10.0     # how long to wait for the cloud when opening a session
MAX_PROMPT_CHARS = 400    # the term hint is a short string (it goes to the cloud with every utterance)
MIN_COMMIT_S = 1.0        # less than this is not charged per utterance: every utterance has overhead
MIN_RESERVE_S = 30.0      # how many seconds of audio the money must cover to open a session
HOLD_S = 60.0             # how many seconds of audio are held at a time (the hold window)
CHECK_S = 20.0            # how often an open session re-checks the account
IDLE_FLUSH_S = 15.0       # this much silence, and the usage is settled and the held amount is returned

_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
_ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# Deletions made by the server itself are marked with this prefix in `event_id`: there is no
# point in showing the client a cloud refusal for such a deletion (the client has already
# removed the item).
OWN_EVENT = "relay-"

# A text frame longer than this is certainly not ours: the biggest honest one is a piece of
# audio.
MAX_FRAME_CHARS = MAX_APPEND_B64 + 4096

# Client events that go to the cloud as they are (without fields).
_PASS = frozenset({"input_audio_buffer.commit", "input_audio_buffer.clear"})

# How many sessions are open now for each account. There is one process (one uvicorn
# worker), so a plain dict is enough.
_open: dict[int, int] = {}

# The shared reserve of several sessions of one account (and of its answers, and of speech
# synthesis) is money held in the database itself (`entitlements.hold`), not process memory:
# four sockets and a parallel question see the remainder already reduced by the others'
# windows, and they cannot split the same remainder. If the process dies, the held amount
# returns at startup (`recover_holds`).


# --------------------------------------------------------------------------
# What we let through to the cloud and what comes back from it
# --------------------------------------------------------------------------


def locked_session(msg: dict[str, Any]) -> tuple[dict[str, Any], str, str] | None:
    """Session setup, rebuilt from what is allowed.

    Returns (message for the cloud, price name, model), or `None` if the client asks for a
    model that is not in `LISTENERS`. Everything else it attached (`instructions`, `tools`,
    `turn_detection` with auto-reply, voice output) is dropped: build from what is allowed
    instead of striking out what is forbidden.
    """
    session = msg.get("session")
    session = session if isinstance(session, dict) else {}
    audio = session.get("audio") if isinstance(session.get("audio"), dict) else {}
    inp = audio.get("input") if isinstance(audio.get("input"), dict) else {}
    asked = inp.get("transcription") if isinstance(inp.get("transcription"), dict) else {}

    model = asked.get("model", DEFAULT_LISTENER)
    tier = LISTENERS.get(model) if isinstance(model, str) else None
    if tier is None:
        return None

    transcription: dict[str, Any] = {"model": model}
    language = asked.get("language")
    if isinstance(language, str) and _LANGUAGE.match(language):
        transcription["language"] = language
    prompt = asked.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        transcription["prompt"] = prompt.strip()[:MAX_PROMPT_CHARS]

    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    # The app cuts utterance boundaries, not the cloud; and with end-of-speech detection turned
                    # off, the session will not start answering on its own.
                    "turn_detection": None,
                    "transcription": transcription,
                },
            },
        },
    }, tier, model


def default_session() -> dict[str, Any]:
    """The default setting: the regular model, language detected from the audio. Any session opens
    with it, and it stays if the client never sends its own."""
    locked = locked_session({})
    assert locked is not None            # DEFAULT_LISTENER is always in LISTENERS
    return locked[0]


def audio_seconds(audio: Any) -> float | None:
    """How many seconds of audio are in a base64 chunk. `None` means it is not audio, it is
    corrupted, or it is too long.

    It is computed by real decoding, not by string length: extra "=" at the tail would reduce
    the count, and the cloud may swallow them (audit 2026-10-16: 30 seconds of audio plus
    millions of "=" were counted as 1 second). Strict decoding accepts only honest base64,
    the kind the app sends.
    """
    if not isinstance(audio, str) or not audio or len(audio) > MAX_APPEND_B64:
        return None
    try:
        raw = base64.b64decode(audio, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw or len(raw) % 2:                   # 16 bits per sample: an odd number of bytes is not audio
        return None
    return len(raw) / BYTES_PER_SECOND


def outgoing(msg: dict[str, Any]) -> dict[str, Any] | None:
    """The part of a cloud event that can be shown to the client. `None` means do not show."""
    kind = msg.get("type", "")
    if not isinstance(kind, str):
        return None
    if kind.startswith("conversation.item.input_audio_transcription."):
        if kind.endswith(".failed"):
            # A refusal for one utterance: the cloud's reason goes to the log, and the client gets a
            # general phrase.
            log.warning("recognition relay: utterance not recognized: %s", msg.get("error"))
            return {"type": kind, "item_id": msg.get("item_id"),
                    "error": {"code": "failed", "message": "the audio wasn't recognized"}}
        return msg
    if kind in ("conversation.item.added", "conversation.item.done"):
        # The client needs only the id: it uses it to trim the session history.
        item = msg.get("item")
        item_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(item_id, str):
            return {"type": kind, "item": {"id": item_id}}
        return None
    if kind == "error":
        return {"type": "error", "error": _plain_error(msg.get("error"))}
    return None


def _plain_error(error: Any) -> dict[str, str]:
    """A cloud refusal without its text: it can contain a piece of a key and the provider's name.

    The details stay in the server log; the client gets a code, by which it decides whether
    the session is alive (`SESSION_IS_OVER` in the app), and a phrase for people.
    """
    code = ""
    if isinstance(error, dict):
        code = str(error.get("code") or error.get("type") or "")
    log.warning("recognition relay: cloud refused: %s", error)
    if code == "session_expired":
        return {"code": "session_expired", "message": "the session has ended"}
    if code in ("invalid_api_key", "insufficient_quota", "billing_not_active"):
        # This is about our key and our account, not about the person; they get a general phrase.
        log.error("recognition relay: problem with the key or the billing at the provider (%s)", code)
        return {"code": "unavailable", "message": "the service is unavailable right now"}
    return {"code": code or "error", "message": "couldn't process the audio"}


# --------------------------------------------------------------------------
# Accounting
# --------------------------------------------------------------------------


class _UpstreamAnswers(Exception):
    """The cloud began answering on its own although the session does not allow it."""


class _OutOfMoney(Exception):
    """Holding money for the audio failed: the balance is not enough. `reason` is text for a person."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Bill:
    """The money of one session: what is held for audio, and what went to the cloud but has not
    yet been charged.

    It works like this: `reserve` holds a window (`HOLD_S` seconds of audio) before a piece
    goes out; `confirm` records what went; `flush` closes the window with the real usage and
    returns the unused part. The window is never smaller than the balance allows: whoever has
    only one piece left on the account gets exactly that piece held. Everything runs on the
    event loop thread (SQLite access is itself under `entitlements._lock`), with no `await`
    between the steps of one operation.
    """

    def __init__(self, db: sqlite3.Connection, account_id: int) -> None:
        self.db = db
        self.account_id = account_id
        self.tier = "normal"
        self.model = DEFAULT_LISTENER
        self._held: entitlements.Hold | None = None   # the hold window that is open now
        self._used = 0                   # how much of the window is taken by audio already accepted (with markup)
        self._owed: dict[tuple[str, str], float] = {}
        self._since_flush = 0.0
        self.last_audio = time.monotonic()
        self.total_s = 0.0
        self.paid_micro = 0
        self.since_commit = 0.0          # how much audio has been accepted since the last commit (for `MIN_COMMIT_S`)

    def reserve(self, seconds: float) -> int:
        """Hold money for a piece of audio that is about to go out. Returns the amount taken, in
        micro-dollars.

        If there is not enough money, `_OutOfMoney`; what accumulated in the previous window has
        been charged by then, and the refusal leaves nothing behind.
        """
        cost = pricing.charged(pricing.stt_cost(seconds, self.tier))
        if self._held is None or self._used + cost > self._held.micro:
            self.flush()                  # the previous window is over: close it with the real usage
            window = max(cost, pricing.charged(pricing.stt_cost(HOLD_S, self.tier)))
            free = entitlements.balance(self.db, self.account_id)
            # The window is no larger than the free remainder but no smaller than the piece itself; if
            # the piece does not fit, `hold` refuses and says why (with the real balance in the text).
            held = entitlements.hold(
                self.db, self.account_id, min(window, free) if free >= cost else cost)
            if not held.allowed:
                raise _OutOfMoney(held.reason)
            self._held, self._used = held, 0
        self._used += cost
        self.last_audio = time.monotonic()
        return cost

    def unreserve(self, cost: int) -> None:
        """The chunk did not go to the cloud (the socket closed): no room in the window is taken for it."""
        self._used = max(0, self._used - cost)

    def shortfall(self) -> float:
        """How many seconds to add to an utterance so that it costs at least `MIN_COMMIT_S`, and reset
        the count.
        """
        short = max(0.0, MIN_COMMIT_S - self.since_commit - 1e-6)
        self.since_commit = 0.0
        return short

    def confirm(self, seconds: float, *, audio: bool = True) -> None:
        """A piece went to the cloud: from this moment it is paid, whatever happens to the
        transcription later.

        `audio=False` is a top-up to the minimum price of an utterance (`shortfall`): there is no
        audio in it.
        """
        if audio:
            self.since_commit += seconds
        key = (self.tier, self.model)
        self._owed[key] = self._owed.get(key, 0.0) + seconds
        self._since_flush += seconds
        self.total_s += seconds
        if self._since_flush >= FLUSH_S:
            self.flush()

    def flush(self) -> None:
        """Close the window with the real spending in one transaction: record, charge, return the
        remainder.
        """
        held, used = self._held, self._used
        owed = self._owed
        self._held, self._used, self._owed, self._since_flush = None, 0, {}, 0.0
        rows = [("stt_seconds", seconds, pricing.stt_cost(seconds, tier), model)
                for (tier, model), seconds in owed.items() if seconds > 0]
        try:
            if held is not None:
                self.paid_micro += entitlements.settle_many(self.db, held, rows)
            else:
                for kind, units, cost, model in rows:
                    self.paid_micro += entitlements.charge(
                        self.db, self.account_id, kind, units, cost, model=model)
        except Exception:
            # Closing failed (the database is busy): everything stays as it was, and the session close
            # will retry. The close is atomic, so a retry will not charge twice.
            self._held, self._used = held, used
            for key, seconds in owed.items():
                self._owed[key] = self._owed.get(key, 0.0) + seconds
            raise

    def standing(self) -> tuple[str, str, int] | None:
        """May this account still listen: `None` means yes, otherwise (code, text, close code).

        It is called before every piece of audio and every `CHECK_S` seconds: a short-lived pass
        lives for fifteen minutes and does not know by itself that the account was closed by the
        owner or deleted, while a socket lives for hours.
        """
        row = self.db.execute(
            "SELECT blocked_at FROM accounts WHERE id = ?", (self.account_id,)).fetchone()
        if row is None or row["blocked_at"] is not None:
            return "account_closed", "account is closed", 4403
        return None

    def recheck(self) -> tuple[str, str, int] | None:
        """A scheduled check of the open session: the account is in place and there is still money."""
        problem = self.standing()
        if problem is not None:
            return problem
        # A silent session must not hold money: we settle the usage, and the held amount is
        # returned.
        if self._held is not None and time.monotonic() - self.last_audio >= IDLE_FLUSH_S:
            self.flush()
        # There is no money at all, counting also what the person currently has in progress: the
        # amount held for their own calls is still theirs, and "the balance is empty" on top of a
        # running answer would be untrue.
        if entitlements.total(self.db, self.account_id) <= 0:
            reason = entitlements.afford(self.db, self.account_id, 1).reason
            return "insufficient_quota", reason or "your balance is empty", 4402
        return None

    def close(self) -> None:
        """End of the session, whatever it is: the spending is charged, the held money is returned. Does
        not raise.
        """
        try:
            self.flush()
        except Exception:
            # Closing must neither hide the reason the session ended nor fail itself. The held amount
            # stayed in the database; `recover_holds` will return it when the server starts.
            log.exception("recognition relay: could not close out the spend for account %s", self.account_id)


class _Items:
    """The conversation items in an open session, so that the history does not grow without limit."""

    def __init__(self) -> None:
        self.ids: list[str] = []
        self._deleted = 0

    def seen(self, item_id: str) -> list[str]:
        """The cloud created an item. Returns those that should be deleted to fit under the ceiling."""
        if item_id in self.ids:
            return []                                # `added`, followed by `done` with the same id
        self.ids.append(item_id)
        if len(self.ids) <= MAX_ITEMS:
            return []
        stale, self.ids = self.ids[:-MAX_ITEMS], self.ids[-MAX_ITEMS:]
        return stale

    def gone(self, item_id: str) -> None:
        """The client deleted the item itself: there is no need to count it any more."""
        if item_id in self.ids:
            self.ids.remove(item_id)

    def own_delete(self, item_id: str) -> dict[str, str]:
        self._deleted += 1
        return {"type": "conversation.item.delete", "item_id": item_id,
                "event_id": f"{OWN_EVENT}{self._deleted}"}


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


async def _say(ws: WebSocket, event: dict[str, Any]) -> None:
    try:
        await ws.send_text(json.dumps(event, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - the client may have already left
        pass


async def _refuse(ws: WebSocket, code: str, message: str, close: int) -> None:
    """Refuse a person in a human way: an event with the reason, then closing."""
    await _say(ws, {"type": "error", "error": {"code": code, "message": message}})
    try:
        await ws.close(code=close)
    except Exception:  # noqa: BLE001
        pass


async def _from_client(ws: WebSocket, up: Any, bill: _Bill, items: _Items) -> None:
    """Client -> cloud: let the allowed through, count the audio.

    It also guards the session: every `CHECK_S` it checks the account (`_Bill.recheck`) and
    closes the session if the account is closed or there is no money, and silence longer than
    `IDLE_S` is also an end. All of it runs in this one task, so the check never cuts into
    the middle of a piece of audio.
    """
    waiting: asyncio.Future | None = None      # expected frame: we do not cancel it between checks
    heard = checked = time.monotonic()          # when the client last spoke / when the account was last checked
    try:
        while True:
            if waiting is None:
                waiting = asyncio.ensure_future(ws.receive())
            now = time.monotonic()
            due = min(heard + IDLE_S, checked + CHECK_S)
            done, _ = await asyncio.wait({waiting}, timeout=max(0.0, due - now))
            now = time.monotonic()
            if now - checked >= CHECK_S:
                checked = now
                problem = bill.recheck()
                if problem is not None:
                    bill.close()
                    await _refuse(ws, *problem)
                    return
            if not done:
                if now - heard >= IDLE_S:
                    await _refuse(ws, "idle", "session closed: no audio for a long time", 1000)
                    return
                continue
            frame, waiting = waiting.result(), None
            heard = now
            if frame["type"] == "websocket.disconnect":
                return
            text = frame.get("text")
            if text is None:
                continue                      # the protocol does not use binary frames
            if len(text) > MAX_FRAME_CHARS:
                await _say(ws, {"type": "error", "error": {
                    "code": "invalid_audio", "message": "the audio wasn't accepted"}})
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")

            if kind == "session.update":
                locked = locked_session(msg)
                if locked is None:
                    await _say(ws, {"type": "error", "error": {
                        "code": "unsupported",
                        "message": "this recognition option isn't offered"}})
                    continue
                payload, bill.tier, bill.model = locked
                await up.send(json.dumps(payload))

            elif kind == "input_audio_buffer.append":
                seconds = audio_seconds(msg.get("audio"))
                if seconds is None:
                    await _say(ws, {"type": "error", "error": {
                        "code": "invalid_audio", "message": "the audio wasn't accepted"}})
                    continue
                # The account was closed while the socket was open: no more audio goes through.
                problem = bill.standing()
                if problem is None:
                    try:
                        cost = bill.reserve(seconds)
                    except _OutOfMoney as e:
                        problem = ("insufficient_quota", e.reason or "your balance is empty", 4402)
                if problem is not None:
                    bill.close()               # what has already gone is charged before the person finds out
                    await _refuse(ws, *problem)
                    return
                try:
                    await up.send(json.dumps({"type": kind, "audio": msg["audio"]}))
                except BaseException:
                    bill.unreserve(cost)       # it did not go to the cloud, so it is not paid
                    raise
                bill.confirm(seconds)

            elif kind in _PASS:
                if kind == "input_audio_buffer.commit":
                    # Each utterance is a separate call to the cloud with its own overhead, and a stream of
                    # short utterances would cost us more than is charged for their audio. We do not charge less
                    # than `MIN_COMMIT_S` per utterance. An honest client's utterances are longer.
                    extra = bill.shortfall()
                    if extra > 0:
                        problem = bill.standing()
                        if problem is None:
                            try:
                                bill.reserve(extra)
                            except _OutOfMoney as e:
                                problem = ("insufficient_quota", e.reason or "your balance is empty", 4402)
                        if problem is not None:
                            bill.close()
                            await _refuse(ws, *problem)
                            return
                        bill.confirm(extra, audio=False)
                else:
                    bill.since_commit = 0.0
                await up.send(json.dumps({"type": kind}))

            elif kind == "conversation.item.delete":
                item_id = msg.get("item_id")
                if isinstance(item_id, str) and _ITEM_ID.match(item_id):
                    items.gone(item_id)
                    await up.send(json.dumps({"type": kind, "item_id": item_id}))

            else:
                # Everything else is none of our business, including whatever would make the cloud answer.
                # We do not close the session: an extra event is no reason to break the conversation.
                await _say(ws, {"type": "error", "error": {
                    "code": "unsupported", "message": "this event isn't supported"}})
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()


async def _to_client(ws: WebSocket, up: Any, items: _Items) -> None:
    """Cloud -> client: only what `outgoing` allows. It also keeps the count of history items."""
    async for raw in up:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        if _is_about_our_delete(msg):
            continue                      # a refusal for a deletion we made ourselves does not go to the client
        kind = msg.get("type")
        if isinstance(kind, str) and kind.startswith("response."):
            # The session is built so that the cloud does not answer on its own (`turn_detection: None`,
            # an answer only on `response.create`, which we do not let through). If it still answers,
            # the assumption no longer matches reality (the cloud changed), and every answer is usage
            # that we do not charge to the person. We tear the session down instead of waiting for our
            # own money to run out.
            log.error("recognition relay: the cloud replies on its own (%s) — session closed", kind)
            with contextlib.suppress(Exception):
                await up.send(json.dumps({"type": "response.cancel"}))
            raise _UpstreamAnswers(kind)
        if msg.get("type") in ("conversation.item.added", "conversation.item.done"):
            item = msg.get("item")
            item_id = item.get("id") if isinstance(item, dict) else None
            if isinstance(item_id, str):
                for stale in items.seen(item_id):
                    await up.send(json.dumps(items.own_delete(stale)))
        event = outgoing(msg)
        if event is not None:
            await ws.send_text(json.dumps(event, ensure_ascii=False))


def _is_about_our_delete(msg: dict[str, Any]) -> bool:
    """A cloud refusal of a deletion that the server itself sent (by the mark in `event_id`)."""
    if msg.get("type") != "error":
        return False
    error = msg.get("error")
    event_id = error.get("event_id") if isinstance(error, dict) else None
    return isinstance(event_id, str) and event_id.startswith(OWN_EVENT)


async def serve(ws: WebSocket, db: sqlite3.Connection, account_id: int) -> None:
    """One open recognition session. `ws` is already accepted and identified."""
    from websockets.asyncio.client import connect

    if _open.get(account_id, 0) >= MAX_SESSIONS:
        await _refuse(ws, "too_many_sessions",
                      "too many open sessions — close the extra ones", 4429)
        return
    if sum(_open.values()) >= MAX_SESSIONS_TOTAL:
        # Not the person's fault: the server is busy. It is the same code that the app already
        # understands as a refusal.
        log.warning("recognition relay: all slots are taken (%d)", MAX_SESSIONS_TOTAL)
        await _refuse(ws, "unavailable", "the service is overloaded right now — try again in a minute", 1013)
        return

    bill = _Bill(db, account_id)
    # Opening a session makes sense only if there is enough for at least half a minute of audio.
    right = entitlements.afford(
        db, account_id, pricing.charged(pricing.stt_cost(MIN_RESERVE_S, "normal")))
    if not right.allowed:
        await _refuse(ws, "insufficient_quota", right.reason or "your balance is empty", 4402)
        return

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        log.error("recognition relay: no key set on the server")
        await _refuse(ws, "unavailable", "the service is unavailable right now", 1013)
        return

    url = f"{UPSTREAM_URL}?model={os.environ.get('ARGUS_STT_REALTIME_MODEL') or REALTIME_MODEL}"
    _open[account_id] = _open.get(account_id, 0) + 1
    started = time.monotonic()
    try:
        try:
            up = await connect(
                url, additional_headers={"Authorization": f"Bearer {key}"},
                open_timeout=OPEN_TIMEOUT_S, close_timeout=1.0,
            )
        except Exception as e:  # noqa: BLE001 - network, key, and provider limits, not the person's fault
            log.warning("recognition relay: the cloud did not open: %s: %s", type(e).__name__, e)
            await _refuse(ws, "unavailable", "the service is unavailable right now", 1013)
            return

        try:
            # The session is in a known configuration right away, even if the client does not send its
            # own.
            try:
                await up.send(json.dumps(default_session()))
            except Exception as e:  # noqa: BLE001 - the cloud closed as soon as it opened
                log.warning("recognition relay: the cloud closed immediately: %s: %s", type(e).__name__, e)
                await _refuse(ws, "unavailable", "the service is unavailable right now", 1013)
                return
            await _say(ws, {"type": "relay.ready"})

            items = _Items()
            pumps = [
                asyncio.create_task(_from_client(ws, up, bill, items), name="stt-from-client"),
                asyncio.create_task(_to_client(ws, up, items), name="stt-to-client"),
            ]
            done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception() if not task.cancelled() else None
                if exc is not None and not _quiet(exc):
                    log.warning("recognition relay: ended with an error (%s): %s",
                                task.get_name(), exc)
        finally:
            try:
                await up.close()
            except Exception:  # noqa: BLE001
                pass
    finally:
        # Whatever ends the session (a break, an error, a task cancel when the server stops), the
        # usage is charged and the held amount is returned. The close is synchronous: a cancel
        # cannot reach it.
        bill.close()
        left = _open.get(account_id, 1) - 1
        if left > 0:
            _open[account_id] = left
        else:
            _open.pop(account_id, None)
        log.info("recognition relay: account %s, session %.0f s, audio %.1f s, charged %s",
                 account_id, time.monotonic() - started, bill.total_s,
                 pricing.money(bill.paid_micro))


def _quiet(exc: BaseException) -> bool:
    """A connection drop is the normal end of a session; there is no need to complain about it in the
    log.
    """
    name = type(exc).__name__
    return name in ("ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError",
                    "WebSocketDisconnect", "CancelledError")


def install(web: FastAPI, db: sqlite3.Connection,
            caller: Callable[..., Awaitable[int]]) -> None:
    """Add `/relay/stt`. `caller` is the same pass check as the other relays use."""

    @web.websocket("/relay/stt")
    async def stt(ws: WebSocket) -> None:
        # The pass arrives in the handshake header, as with the other endpoints. Without a valid
        # one we close before accepting: from outside this is an ordinary 403 refusal, and no
        # conversation is held with a stranger.
        try:
            account_id = await caller(ws.headers.get("authorization", ""))
        except HTTPException:
            await ws.close(code=1008)
            return
        await ws.accept()
        await serve(ws, db, account_id)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
