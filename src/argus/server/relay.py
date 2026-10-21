"""Relays: expensive calls to models and to synthesis go through the server, not straight
from someone else's computer with a key. The keys (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`) are read here, from the server's environment,
and do not get into the build that is distributed at all.

Each call here is paid and is charged to the balance (`pricing.py`). Before the call, the
worst case of the request is held for the duration of the call (`entitlements.hold`, a
single conditional `UPDATE`; parallel calls of the same account already see the reduced
balance). After the call, the real price is taken from the number of tokens or characters
the provider returned, and the remainder is given back. If the provider refuses,
everything is returned.

Only models from the price list are sold (`pricing.MODELS`). This is not politeness but a
lock: the model name comes from the client, and without a list anyone could demand the
most expensive one available to us, and we would learn the price from the bill.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field, StringConstraints, field_validator

from argus.server import entitlements, pricing, relay_stt, throttle

log = logging.getLogger(__name__)

# The maximum for one request. This is the same thing that protects the server from someone
# cutting out the checks in the client and sending whatever they like straight here.
#
# The numbers agree with what the app really sends, otherwise a question with materials
# would get a refusal where the person did nothing wrong: materials are by default up to
# 120,000 characters and 40 page images (`assist/settings.py`), plus the feed, the question,
# and the screen frame (`assist/prompt.py`). Size does not make anything more expensive
# unnoticed: the price is calculated by tokens, and whether the ceiling is affordable is
# decided by the hold (`entitlements.hold`).
MAX_SYSTEM_CHARS = 200_000
MAX_PROMPT_CHARS = 60_000
MAX_TTS_CHARS = 4_000
MAX_IMAGES = 42
MAX_IMAGE_B64 = 4_000_000          # base64 characters in one image (about 3 MB JPEG)
MAX_ANSWER_TOKENS = 16_000

# If the provider is silent for longer, the call failed and costs the person nothing.
# Without an explicit timeout, the library client would wait for minutes, and the handler
# would keep its place and its hold.
UPSTREAM_TIMEOUT_S = 120.0
UPSTREAM_DEADLINE_S = 150.0        # ceiling for the whole call including the retry

# A call that broke off not at once but after a long wait costs the person nothing (the
# hold is returned), but in that time the provider may have produced, and counted, a
# significant part of the answer. So such failures are counted: two in half an hour for one
# account, and new questions are closed for that time. For an honest person a time-based
# failure is rare and not in a row; for someone who deliberately asks for a known long
# answer so that it "fails" for free, it is not (audit 2026-10-16).
SLOW_FAILURE_S = 40.0
SLOW_FAILURES_ALLOWED = ((2, 30 * 60),)

# How many paid calls may run at once: per account and for the whole server. This is a
# second layer after the hold: on an account with a large sum, the hold would let through
# a thousand parallel calls, and each holds memory and a socket. For answers it is five: a
# conversation recap sends up to three requests at once (`recap.engine.PARALLEL`), and the
# person's question must not be refused in the meantime.
MAX_INFLIGHT_BRAIN = 5
MAX_INFLIGHT_TTS = 4
MAX_INFLIGHT_TOTAL = 32

# What may be requested from speech synthesis. The call address is assembled from these
# fields, so everything that is not in the lists is cut off at the input and not forwarded
# with our key to wherever.
TTS_MODELS = frozenset({
    "eleven_multilingual_v2", "eleven_multilingual_v1", "eleven_monolingual_v1",
    "eleven_flash_v2_5", "eleven_flash_v2", "eleven_turbo_v2_5", "eleven_turbo_v2",
    "eleven_v3",
})
# Only mp3: the answer is returned as `audio/mpeg`.
TTS_FORMATS = frozenset({
    "mp3_22050_32", "mp3_44100_32", "mp3_44100_64", "mp3_44100_96", "mp3_44100_128",
    "mp3_44100_192",
})


class BrainAskRequest(BaseModel):
    provider: str = Field(pattern="^(openai-api|anthropic-api)$")
    model: str = Field(min_length=1, max_length=100)
    system: str = Field(default="", max_length=MAX_SYSTEM_CHARS)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    image_b64: str | None = Field(default=None, max_length=MAX_IMAGE_B64)
    pinned_b64: list[Annotated[str, StringConstraints(max_length=MAX_IMAGE_B64)]] = Field(
        default_factory=list, max_length=MAX_IMAGES)
    max_tokens: int = Field(default=1024, ge=1, le=MAX_ANSWER_TOKENS)
    effort: str = Field(default="low", max_length=20)


class BrainAskResponse(BaseModel):
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    # The answer was cut off by the token ceiling: the text is incomplete, but it is paid in
    # full. Set only when the provider stopped precisely because of the ceiling (not because of
    # a content filter and not for an unknown reason). The client reads `body.get("truncated")`;
    # if the field is missing, it treats it as false.
    truncated: bool = False


class TTSRequest(BaseModel):
    # The voice is substituted into the path of the ElevenLabs address: "../" or "?" in it
    # would redirect the call with our key to another, also paid, endpoint. Real ids are
    # letters and digits.
    voice_id: str = Field(pattern=r"^[A-Za-z0-9]{10,40}$")
    model_id: str = Field(default="eleven_multilingual_v2", max_length=64)
    output_format: str = Field(default="mp3_44100_128", max_length=32)
    text: str = Field(min_length=1, max_length=MAX_TTS_CHARS)
    stability: float = Field(default=0.5, ge=0.0, le=1.0)
    similarity_boost: float = Field(default=0.75, ge=0.0, le=1.0)

    @field_validator("model_id")
    @classmethod
    def _known_model(cls, value: str) -> str:
        if value not in TTS_MODELS:
            raise ValueError("this voice model isn't offered")
        return value

    @field_validator("output_format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        if value not in TTS_FORMATS:
            raise ValueError("this audio format isn't offered")
        return value


async def _ask_openai(req: BrainAskRequest) -> BrainAskResponse:
    from openai import AsyncOpenAI

    content: list[dict[str, Any]] = [
        {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b}", "detail": "auto"}
        for b in req.pinned_b64
    ]
    if req.image_b64:
        content.append({
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{req.image_b64}",
            "detail": "auto",
        })
    content.append({"type": "input_text", "text": req.prompt})

    async with AsyncOpenAI(timeout=UPSTREAM_TIMEOUT_S, max_retries=1) as client:
        response = await client.responses.create(
            model=req.model,
            instructions=req.system or None,
            input=[{"role": "user", "content": content}],
            reasoning={"effort": req.effort},
            max_output_tokens=req.max_tokens,
        )
    usage = response.usage
    return BrainAskResponse(
        text=(response.output_text or "").strip(),
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        truncated=_openai_hit_the_limit(response),
    )


def _openai_hit_the_limit(response: Any) -> bool:
    """The answer was cut off by the token ceiling, and not by something else.

    The `incomplete` status has two causes: the model hit `max_output_tokens` (reasoning
    counts too), or a content filter fired. In the second case the text is incomplete not
    because the request was too short, and the advice "increase the answer length" would be
    untrue. An unknown cause, or none, is also not "cut off by the ceiling": we state with
    confidence only what we know.
    """
    if getattr(response, "status", None) != "incomplete":
        return False
    details = getattr(response, "incomplete_details", None)
    reason = details.get("reason") if isinstance(details, dict) else getattr(details, "reason", None)
    return reason == "max_output_tokens"


async def _ask_anthropic(req: BrainAskRequest) -> BrainAskResponse:
    from anthropic import AsyncAnthropic

    content: list[dict[str, Any]] = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
        for b in req.pinned_b64
    ]
    if req.image_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": req.image_b64},
        })
    content.append({"type": "text", "text": req.prompt})

    async with AsyncAnthropic(timeout=UPSTREAM_TIMEOUT_S, max_retries=1) as client:
        response = await client.messages.create(
            model=req.model,
            system=req.system or "",
            max_tokens=req.max_tokens,
            messages=[{"role": "user", "content": content}],
        )
    text = "".join(b.text for b in response.content if b.type == "text")
    return BrainAskResponse(
        text=text.strip(),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        truncated=getattr(response, "stop_reason", None) == "max_tokens",
    )


def install(web: FastAPI, db: sqlite3.Connection, caller: Callable[..., Any]) -> None:
    """Add `/relay/*` to an already built accounts server.

    `caller` is the same entry dependency as for `/me` and the other endpoints
    (`server/app.py`): one and the same pass works for both.
    """

    # How many calls are running right now: (kind, account) -> count. The counter lives on the
    # event loop and does not wait between the check and the increment, so no lock is needed.
    inflight: dict[tuple[str, int], int] = {}
    slow_failures = throttle.Window(SLOW_FAILURES_ALLOWED)

    @contextmanager
    def slot(kind: str, account_id: int, limit: int):
        """A slot for a single paid call; if there is no room, an immediate 429, before any work."""
        key = (kind, account_id)
        if inflight.get(key, 0) >= limit or sum(inflight.values()) >= MAX_INFLIGHT_TOTAL:
            raise HTTPException(429, "too many requests at once — wait for the answer",
                                headers={"Retry-After": "3"})
        inflight[key] = inflight.get(key, 0) + 1
        try:
            yield
        finally:
            left = inflight[key] - 1
            if left > 0:
                inflight[key] = left
            else:
                del inflight[key]

    @web.post("/relay/brain/ask", response_model=BrainAskResponse)
    async def brain_ask(
        payload: BrainAskRequest, account_id: int = Depends(caller)
    ) -> BrainAskResponse:
        try:
            price = pricing.price_of(payload.model)
        except pricing.UnknownModel:
            log.warning("relay: model not in the price list: %r", payload.model)
            raise HTTPException(400, "this kind of answer isn't offered") from None
        if price.provider != payload.provider:
            log.warning("relay: model: %r, not at %s", payload.model, payload.provider)
            raise HTTPException(400, "the request wasn't accepted")

        images = len(payload.pinned_b64) + (1 if payload.image_b64 else 0)
        ceiling = pricing.charged(pricing.brain_ceiling(
            payload.model, len(payload.system) + len(payload.prompt),
            images, payload.max_tokens,
            text_bytes=len(payload.system.encode()) + len(payload.prompt.encode()),
        ))
        with slot("brain", account_id, MAX_INFLIGHT_BRAIN):
            pause = slow_failures.wait(f"a{account_id}")
            if pause > 0:
                raise HTTPException(
                    429, f"several replies in a row did not arrive in time — wait {throttle.wait_text(pause)}",
                    headers={"Retry-After": str(int(pause) + 1)})
            # Not "check, then pay" but hold before the call: while the model is thinking, parallel
            # requests of the same account already see the reduced balance.
            held = entitlements.hold(db, account_id, ceiling)
            if not held.allowed:
                raise HTTPException(402, held.reason or "no access")
            settled = False
            started = time.monotonic()
            try:
                ask = _ask_openai if payload.provider == "openai-api" else _ask_anthropic
                try:
                    out = await asyncio.wait_for(ask(payload), UPSTREAM_DEADLINE_S)
                except Exception as e:  # noqa: BLE001 - network and provider limits, not our error
                    log.warning("model relay refused (%s): %s: %s",
                                payload.provider, type(e).__name__, e)
                    if time.monotonic() - started >= SLOW_FAILURE_S:
                        slow_failures.add(f"a{account_id}")
                    raise HTTPException(502, "couldn't get an answer right now") from None
                entitlements.settle(
                    db, held, "brain_tokens", out.input_tokens + out.output_tokens,
                    pricing.brain_cost(payload.model, out.input_tokens, out.output_tokens),
                    model=payload.model,
                )
                settled = True
                return out
            finally:
                if not settled:         # refusal, timeout, cancellation: this costs the person nothing
                    entitlements.release(db, held)

    @web.post("/relay/tts/synthesize")
    async def tts_synthesize(payload: TTSRequest, account_id: int = Depends(caller)):
        import httpx

        cost = pricing.tts_cost(len(payload.text))
        with slot("tts", account_id, MAX_INFLIGHT_TTS):
            held = entitlements.hold(db, account_id, pricing.charged(cost))
            if not held.allowed:
                raise HTTPException(402, held.reason or "no access")
            settled = False
            try:
                api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
                if not api_key:
                    log.warning("speech relay: key not set")
                    raise HTTPException(500, "voice replies are unavailable right now")

                # The voice id has already been checked (`TTSRequest`), but the address is still built with
                # escaping: the path must not depend on someone loosening the rule.
                url = ("https://api.elevenlabs.io/v1/text-to-speech/"
                       f"{quote(payload.voice_id, safe='')}/stream")
                body = {
                    "text": payload.text,
                    "model_id": payload.model_id,
                    "voice_settings": {
                        "stability": payload.stability,
                        "similarity_boost": payload.similarity_boost,
                    },
                }
                headers = {"xi-api-key": api_key, "Content-Type": "application/json",
                           "Accept": "audio/mpeg"}
                params = {"output_format": payload.output_format}
                try:
                    async with httpx.AsyncClient(timeout=30.0) as http:
                        r = await http.post(url, json=body, headers=headers, params=params)
                except httpx.HTTPError as e:
                    log.warning("speech relay: no connection: %s", e)
                    raise HTTPException(502, "voice replies are unavailable right now") from None
                if r.status_code >= 400:
                    log.warning("speech relay refused: %s %s", r.status_code, r.text[:200])
                    raise HTTPException(502, "voice replies are unavailable right now")
                entitlements.settle(db, held, "tts_chars", len(payload.text), cost,
                                    model=payload.model_id)
                settled = True
                return Response(r.content, media_type="audio/mpeg")
            finally:
                if not settled:
                    entitlements.release(db, held)

    # Speech recognition is not request-response but a long two-way socket, so it lives in its
    # own module (`relay_stt.py`); the pass and the balance are the same.
    relay_stt.install(web, db, caller)
