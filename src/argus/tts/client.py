"""A thin ElevenLabs client: a handful of requests, not a whole SDK.

The official SDKs (`openai`, `anthropic`) are kept in the project for simple `ask()`
calls; for the streaming API `realtime_openai.py` already takes a raw `websockets`
instead of a ready-made helper. The same principle here: a few HTTP endpoints are not
worth a dependency the weight of a whole library, and `httpx` is in the project anyway.

⚠️ Paths, headers, and the shape of the request body follow the ElevenLabs documentation
at the time of writing. Vendor APIs drift; if any of this breaks, check their current
docs first and do not treat it as a typo. `synthesize`/`add_voice` are as they were,
without a live check. `list_voices` and `search_shared_voices` were checked against the
live API on 2026-09-15 (the key from this project's `.env`; `GET /v1/voices` and
`GET /v1/shared-voices` both returned the expected shape). `add_shared_voice` follows the
documentation and was not called live: it uses up a custom-voice slot in the account, and
testing it by spending someone else's limit is not the thing to do.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argus.tts.settings import TTSSettings, VoiceProfile

log = logging.getLogger(__name__)

BASE = "https://api.elevenlabs.io/v1"

# Synthesizing a large answer must not hang the pipeline for long: this is background work,
# not something a person waits for while staring at an empty screen.
TIMEOUT_S = 30.0


async def synthesize(
    text: str, profile: VoiceProfile, settings: TTSSettings, api_key: str
) -> bytes:
    """Speak the text in the cloned voice. Raises an exception on refusal: the caller
    (`tts/reply.py`) decides what to do with it, since the pipeline must not fall over because
    ElevenLabs is unavailable.
    """
    import httpx

    url = f"{BASE}/text-to-speech/{profile.voice_id}/stream"
    payload = {
        "text": text,
        "model_id": settings.model_id,
        "voice_settings": {
            "stability": profile.stability,
            "similarity_boost": profile.similarity_boost,
        },
    }
    headers = {"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"}
    params = {"output_format": settings.output_format}

    async with (
        httpx.AsyncClient(timeout=TIMEOUT_S) as http,
        http.stream("POST", url, json=payload, headers=headers, params=params) as response,
    ):
        response.raise_for_status()
        chunks = [chunk async for chunk in response.aiter_bytes()]
    return b"".join(chunks)


async def synthesize_via_relay(
    text: str, profile: VoiceProfile, settings: TTSSettings, account,
) -> bytes:
    """The same as `synthesize`, but through the accounts server: the ElevenLabs key stays there
    (`server/relay.py`), not on this computer. Raises `AccountError` on refusal, the same way
    as `Account._call`.

    `_call` is a synchronous request, so it goes to a thread: in an `async` function it would
    hold the whole event loop for the duration of synthesizing each phrase, and the dashboard
    would freeze along with the speech (see the same in `RelayProvider.ask`).

    The thread is a daemon (`_in_daemon_thread`), not `asyncio.to_thread`: `_call` waits for a
    long time (the server's reply takes up to about 160 seconds), and the process waits for
    `to_thread` threads on exit, so a window closed in the middle of synthesis would keep the
    program alive, and a new launch would run into its lock. A late result is silently
    dropped.
    """
    from argus.brain.relay_api import _in_daemon_thread

    r = await _in_daemon_thread(
        account._call,
        "POST", "/relay/tts/synthesize", auth=True,
        json={
            "voice_id": profile.voice_id,
            "model_id": settings.model_id,
            "output_format": settings.output_format,
            "text": text,
            "stability": profile.stability,
            "similarity_boost": profile.similarity_boost,
        },
    )
    return r.content


async def add_voice(name: str, sample: bytes, filename: str, api_key: str) -> str:
    """Clone a voice from a sample (Instant Voice Cloning). Returns `voice_id`."""
    import httpx

    headers = {"xi-api-key": api_key}
    files = {"files": (filename, sample, "application/octet-stream")}
    data = {"name": name}

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
        response = await http.post(
            f"{BASE}/voices/add", headers=headers, data=data, files=files
        )
        response.raise_for_status()
        body = response.json()

    voice_id = body.get("voice_id")
    if not voice_id:
        raise ValueError(f"ElevenLabs did not return a voice_id: {body}")
    return voice_id


async def list_voices(api_key: str) -> list[dict]:
    """Voices already available to the account: ElevenLabs premade and its own (clones bought in
    the marketplace, previously added from the shared library). They can be used for speech
    right now, without a single extra request, unlike `search_shared_voices`, from which a
    voice must first be added through `add_shared_voice`.
    """
    import httpx

    headers = {"xi-api-key": api_key}
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
        response = await http.get(f"{BASE}/voices", headers=headers)
        response.raise_for_status()
        body = response.json()
    return body.get("voices", [])


async def search_shared_voices(
    api_key: str,
    *,
    language: str = "",
    gender: str = "",
    search: str = "",
    page_size: int = 30,
    page: int = 0,
) -> list[dict]:
    """The ElevenLabs shared voice library: tens of thousands of voices in different languages,
    not only what is already in the account. Browsing is free; to speak with a particular
    voice it must first be added to the account with `add_shared_voice`, and that is no longer
    free (a custom-voice slot).
    """
    import httpx

    headers = {"xi-api-key": api_key}
    params: dict[str, str | int] = {"page_size": page_size, "page": page}
    if language:
        params["language"] = language
    if gender:
        params["gender"] = gender
    if search:
        params["search"] = search

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
        response = await http.get(f"{BASE}/shared-voices", headers=headers, params=params)
        response.raise_for_status()
        body = response.json()
    return body.get("voices", [])


async def add_shared_voice(public_owner_id: str, voice_id: str, name: str, api_key: str) -> str:
    """Add a voice from the shared library to your account. Without this step, synthesis with it
    returns a refusal even if `voice_id` is known. This is the same thing that used to be done
    by hand in `add-voice` (`cli.py`), with the "Add to my voices" button on the website. It
    takes a custom-voice slot, and their number is limited by the ElevenLabs plan.
    """
    import httpx

    headers = {"xi-api-key": api_key}
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
        response = await http.post(
            f"{BASE}/voices/add/{public_owner_id}/{voice_id}",
            headers=headers,
            json={"new_name": name},
        )
        response.raise_for_status()
        body = response.json()
    return body.get("voice_id") or voice_id
