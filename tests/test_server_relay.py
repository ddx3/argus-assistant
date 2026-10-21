"""Relays: access only with a pass, keys never go out, every call charges money.

We do not hit the real OpenAI/Anthropic/ElevenLabs: `_ask_openai`/`_ask_anthropic` and
the call to ElevenLabs are replaced, and the tests check what surrounds them: 401 without
a sign-in, 400 for a model that is not in the price list, 402 without money, charging by
the number of tokens and characters, and a failed upstream not costing the person a
cent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from argus.server import entitlements, pricing, relay
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "a-fairly-long-password"
MODEL = "gpt-5.6-luna"


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


@pytest.fixture
def http(db):
    with TestClient(build(db, SECRET)) as client:
        yield client


def _access(http) -> str:
    body = http.post(
        "/auth/register",
        json={"email": "user@example.com", "password": PASSWORD, "terms": "t"},
    ).json()
    return body["access"]


def _ask(http, access, **over):
    body = {"provider": "openai-api", "model": MODEL, "prompt": "héllo"}      # non-ASCII on purpose: bytes differ from characters
    body.update(over)
    return http.post("/relay/brain/ask", headers={"Authorization": f"Bearer {access}"},
                     json=body)


def _no_money(db) -> None:
    db.execute("UPDATE accounts SET balance_micro = 0")
    db.commit()


def test_brain_ask_needs_auth(http):
    r = http.post("/relay/brain/ask", json={
        "provider": "openai-api", "model": MODEL, "prompt": "héllo",
    })
    assert r.status_code == 401


def test_brain_ask_calls_upstream_and_charges_the_balance(http, db, monkeypatch):
    access = _access(http)
    before = entitlements.balance(db, 1)

    async def fake_openai(req):
        assert req.prompt == "héllo"
        return relay.BrainAskResponse(text="answer", input_tokens=1000, output_tokens=500)

    monkeypatch.setattr(relay, "_ask_openai", fake_openai)

    r = _ask(http, access)
    assert r.status_code == 200
    assert r.json() == {"text": "answer", "input_tokens": 1000, "output_tokens": 500,
                        "truncated": False}

    # Tokens are recorded for the report, and the money follows the price list, with markup.
    assert entitlements.spent(db, 1, "brain_tokens") == 1500
    expected = pricing.charged(pricing.brain_cost(MODEL, 1000, 500))
    assert entitlements.balance(db, 1) == before - expected
    row = db.execute("SELECT model, cost_micro, charged_micro FROM usage").fetchone()
    assert row["model"] == MODEL
    assert row["charged_micro"] == expected
    assert row["cost_micro"] == pricing.brain_cost(MODEL, 1000, 500)


def test_a_dearer_model_costs_more_for_the_same_tokens(http, db, monkeypatch):
    access = _access(http)
    entitlements.credit(db, 1, 20 * pricing.MICRO, "topup")

    async def fake(req):
        return relay.BrainAskResponse(text="ok", input_tokens=1000, output_tokens=500)

    monkeypatch.setattr(relay, "_ask_openai", fake)

    b0 = entitlements.balance(db, 1)
    _ask(http, access, model="gpt-5.6-luna")
    cheap = b0 - entitlements.balance(db, 1)
    b1 = entitlements.balance(db, 1)
    _ask(http, access, model="gpt-5.6-terra")
    dear = b1 - entitlements.balance(db, 1)
    assert dear == 10 * cheap


def test_a_model_not_in_the_price_list_is_refused(http, db, monkeypatch):
    """The model name comes from the client: without a list, anyone could ask for the most
    expensive one available to us."""
    access = _access(http)

    async def fake(req):
        raise AssertionError("must not call the provider for a model that is not in the price list")

    monkeypatch.setattr(relay, "_ask_openai", fake)

    r = _ask(http, access, model="gpt-the-most-expensive")
    assert r.status_code == 400
    assert "isn't offered" in r.json()["detail"]


def test_a_model_cannot_be_sold_under_the_wrong_provider(http, monkeypatch):
    access = _access(http)

    async def fake(req):
        raise AssertionError("must not call the provider")

    monkeypatch.setattr(relay, "_ask_anthropic", fake)
    monkeypatch.setattr(relay, "_ask_openai", fake)

    r = _ask(http, access, provider="anthropic-api", model=MODEL)
    assert r.status_code == 400


def test_anthropic_models_are_sold_too(http, db, monkeypatch):
    access = _access(http)

    async def fake(req):
        return relay.BrainAskResponse(text="ok", input_tokens=100, output_tokens=50)

    monkeypatch.setattr(relay, "_ask_anthropic", fake)

    r = _ask(http, access, provider="anthropic-api", model="claude-haiku-4-5")
    assert r.status_code == 200


def test_brain_ask_denied_without_money(http, db, monkeypatch):
    access = _access(http)
    _no_money(db)

    async def fake_openai(req):
        raise AssertionError("must not call the provider without money")

    monkeypatch.setattr(relay, "_ask_openai", fake_openai)

    r = _ask(http, access)
    assert r.status_code == 402
    assert "top up" in r.json()["detail"]


def test_brain_ask_denied_when_the_worst_case_does_not_fit(http, db, monkeypatch):
    """A balance short by a cent must not let an expensive request through: we would be the ones
    paying.
    """
    access = _access(http)
    db.execute("UPDATE accounts SET balance_micro = 1")
    db.commit()

    async def fake_openai(req):
        raise AssertionError("must not call the provider if the ceiling does not fit")

    monkeypatch.setattr(relay, "_ask_openai", fake_openai)

    r = _ask(http, access, model="gpt-5.6-terra", max_tokens=8000)
    assert r.status_code == 402


def test_brain_ask_upstream_failure_is_502_and_costs_nothing(http, db, monkeypatch):
    access = _access(http)
    before = entitlements.balance(db, 1)

    async def broken(req):
        raise RuntimeError("limit exhausted")

    monkeypatch.setattr(relay, "_ask_openai", broken)

    r = _ask(http, access)
    assert r.status_code == 502
    assert entitlements.balance(db, 1) == before
    assert db.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0


def test_a_last_answer_on_a_tiny_balance_is_still_served(http, db, monkeypatch):
    """There is enough for the ceiling, so we answer, and we charge the real price, not the
    ceiling.
    """
    access = _access(http)
    ceiling = pricing.charged(pricing.brain_ceiling(
        MODEL, len("héllo"), 0, 1024, text_bytes=len("héllo".encode())))
    db.execute("UPDATE accounts SET balance_micro = ?", (ceiling,))
    db.commit()

    async def fake(req):
        return relay.BrainAskResponse(text="ok", input_tokens=10, output_tokens=5)

    monkeypatch.setattr(relay, "_ask_openai", fake)

    assert _ask(http, access).status_code == 200
    assert 0 < entitlements.balance(db, 1) < ceiling


# --------------------------------------------------------------------------
# Speech synthesis
# --------------------------------------------------------------------------


def _fake_elevenlabs(monkeypatch, status=200):
    class FakeResponse:
        status_code = status
        content = b"mp3-bytes"
        text = "provider error"

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)


VOICE = "21m00Tcm4TlvDq8ikWAM"      # ElevenLabs voice IDs are twenty letters and digits


def _speak(http, access, text="héllo", **over):
    return http.post(
        "/relay/tts/synthesize",
        headers={"Authorization": f"Bearer {access}"},
        json={"voice_id": VOICE, "text": text, **over},
    )


def test_tts_synthesize_needs_money(http, db):
    access = _access(http)
    _no_money(db)
    assert _speak(http, access).status_code == 402


def test_tts_is_open_to_everyone_with_money(http, db, monkeypatch):
    """No "voice plans": have money, have a voice."""
    access = _access(http)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-key")
    _fake_elevenlabs(monkeypatch)
    assert _speak(http, access).status_code == 200


def test_tts_synthesize_needs_server_key(http, db, monkeypatch):
    access = _access(http)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert _speak(http, access).status_code == 500


def test_tts_synthesize_charges_by_characters(http, db, monkeypatch):
    access = _access(http)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-key")
    _fake_elevenlabs(monkeypatch)
    before = entitlements.balance(db, 1)

    text = "héllo" * 100
    r = _speak(http, access, text)
    assert r.status_code == 200
    assert r.content == b"mp3-bytes"
    assert entitlements.spent(db, 1, "tts_chars") == len(text)
    assert entitlements.balance(db, 1) == before - pricing.charged(pricing.tts_cost(len(text)))


def test_tts_denied_when_the_text_costs_more_than_the_balance(http, db, monkeypatch):
    access = _access(http)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-key")
    _fake_elevenlabs(monkeypatch)
    db.execute("UPDATE accounts SET balance_micro = 1")
    db.commit()
    assert _speak(http, access, "héllo" * 500).status_code == 402


def test_a_failed_synthesis_costs_nothing(http, db, monkeypatch):
    access = _access(http)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-key")
    _fake_elevenlabs(monkeypatch, status=500)
    before = entitlements.balance(db, 1)

    assert _speak(http, access).status_code == 502
    assert entitlements.balance(db, 1) == before


# --------------------------------------------------------------------------
# What can be asked of speech synthesis
# --------------------------------------------------------------------------


@pytest.mark.parametrize("voice", [
    "../../sound-generation", "a/../../sound-generation?x=1", "abc", "x" * 41,
    "21m00Tcm4Tlv#DqWAM", "21m00Tcm4TlvDq?foo=1", "21m00Tcm4TlvDq/stream", "21m00Tcm 4TlvDq8ik",
    "../../../v1/user/subscription", "21m00Tcm4TlvDq%2e%2e", "vöice-vöice-vöice",
])
def test_a_voice_that_could_redirect_the_call_is_refused(http, db, monkeypatch, voice):
    """The voice id is substituted into the URL path: "../" or "?" would redirect the paid call,
    with our key, to a different ElevenLabs endpoint."""
    access = _access(http)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-key")
    _fake_elevenlabs(monkeypatch)
    before = entitlements.balance(db, 1)
    r = http.post("/relay/tts/synthesize", headers={"Authorization": f"Bearer {access}"},
                  json={"voice_id": voice, "text": "hello"})
    assert r.status_code == 422
    assert entitlements.balance(db, 1) == before


@pytest.mark.parametrize("over", [
    {"model_id": "eleven_sound_effects"}, {"model_id": "../x"},
    {"output_format": "pcm_44100"}, {"output_format": "mp3_44100_128&foo=bar"},
])
def test_an_unknown_model_or_format_of_speech_is_refused(http, over):
    access = _access(http)
    assert _speak(http, access, **over).status_code == 422


def test_the_call_goes_to_exactly_the_voice_that_was_asked_for(http, monkeypatch):
    access = _access(http)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-key")
    sent = {}

    class Capturing:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kw):
            sent.update(url=url, params=kw.get("params"))

            class R:
                status_code = 200
                content = b"mp3"
                text = ""

            return R()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", Capturing)
    r = _speak(http, access, model_id="eleven_flash_v2_5", output_format="mp3_22050_32")
    assert r.status_code == 200
    assert sent["url"] == f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}/stream"
    assert sent["params"] == {"output_format": "mp3_22050_32"}


def test_the_default_voice_settings_of_the_app_are_on_sale():
    """The model and format the app sends by default must pass."""
    from argus.tts.settings import TTSSettings

    settings = TTSSettings()
    assert settings.model_id in relay.TTS_MODELS
    assert settings.output_format in relay.TTS_FORMATS
