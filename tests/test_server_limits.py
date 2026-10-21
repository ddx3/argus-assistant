"""Request size: the body is limited BEFORE parsing, and what the app sends fits.

Two sides of one coin. The server must not read gigabytes from an anonymous client
(FastAPI parses the body before it checks the pass), and the server must not refuse an app
that has violated nothing: the default materials are 120 thousand characters and 40
picture pages, and a question with them must reach the model.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from argus.assist.prompt import compose
from argus.assist.settings import AssistSettings, Materials
from argus.core.config import Config
from argus.server import accounts, entitlements, limits, pricing, relay
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "a-fairly-long-password"
MODEL = "gpt-5.6-luna"


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


@pytest.fixture
def login(db):
    return accounts.register(db, SECRET, "user@example.com", PASSWORD)


def bearer(login) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", f"Bearer {login.access}".encode())]


async def raw_call(app, path: str, chunks: list[bytes], headers=(), *, declare=True):
    """One request straight into the app. Returns (status, response body, how many chunks of the
    body the app managed to read)."""
    sent_headers = [(b"content-type", b"application/json"), *headers]
    if declare:
        sent_headers.append((b"content-length", str(sum(map(len, chunks))).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "scheme": "http", "headers": sent_headers, "server": ("server", 80),
        "client": ("198.51.100.1", 5000),
    }
    consumed = 0

    async def receive():
        nonlocal consumed
        if consumed >= len(chunks):
            return {"type": "http.disconnect"}
        chunk = chunks[consumed]
        consumed += 1
        return {"type": "http.request", "body": chunk, "more_body": consumed < len(chunks)}

    status = 0
    body = b""

    async def send(message):
        nonlocal status, body
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await app(scope, receive, send)
    return status, body, consumed


def ask(**over) -> dict:
    body = {"provider": "openai-api", "model": MODEL, "prompt": "hello"}
    body.update(over)
    return body


def blob(chars: int) -> str:
    return "A" * chars


# --------------------------------------------------------------------------
# Body limit
# --------------------------------------------------------------------------


async def test_a_declared_oversize_body_is_refused_before_anything_reads_it(db, monkeypatch):
    async def never(*a, **kw):
        raise AssertionError("the body reached parsing")

    monkeypatch.setattr(accounts, "sign_in_async", never)
    chunks = [b"x" * 70_000]
    status, body, consumed = await raw_call(build(db, SECRET), "/auth/sign-in", chunks)
    assert status == 413
    assert consumed == 0, "the server read a body it will certainly not accept"
    assert "too large" in json.loads(body)["detail"]


async def test_a_body_without_a_length_is_cut_off_while_it_is_still_arriving(db):
    """Body in chunks, without `Content-Length`: it is counted on the fly, not after reading all
    of it.
    """
    chunks = [b"x" * 10_000 for _ in range(200)]
    status, body, consumed = await raw_call(
        build(db, SECRET), "/auth/register", chunks, declare=False)
    assert status == 413
    assert consumed <= 8, f"read {consumed} chunks out of 200"
    assert json.loads(body)["detail"]


@pytest.mark.parametrize("headers", [
    [],                                                        # without a pass
    [(b"authorization", b"Bearer garbage")],                   # garbage in the header
    [(b"authorization", "Bearer é.é".encode("latin-1"))],      # and non-ASCII
])
async def test_without_a_real_pass_even_the_question_route_has_the_small_limit(db, headers):
    chunks = [b"x" * 200_000]
    status, _, consumed = await raw_call(
        build(db, SECRET), "/relay/brain/ask", chunks, headers)
    assert status == 413 and consumed == 0


async def test_a_forged_signature_does_not_open_the_big_limit(db, login):
    payload = login.access.split(".")[0]
    forged = [(b"authorization", f"Bearer {payload}.{'A' * 43}".encode())]
    status, _, _ = await raw_call(build(db, SECRET), "/relay/brain/ask",
                                  [b"x" * 200_000], forged)
    assert status == 413


async def test_the_big_limit_is_for_the_question_route_only(db, login):
    status, _, _ = await raw_call(build(db, SECRET), "/auth/password",
                                  [b"x" * 200_000], bearer(login))
    assert status == 413


async def test_a_signed_in_person_may_send_a_big_question_but_not_a_giant_one(db, login):
    app = build(db, SECRET)
    big = json.dumps(ask(system=blob(150_000), image_b64=blob(3_000_000))).encode()
    status, _, _ = await raw_call(app, "/relay/brain/ask", [big], bearer(login))
    assert status != 413, "a question with materials must not run into the body limit"

    giant = [b"x" * 1_000_000 for _ in range(limits.BIG_BODY // 1_000_000 + 2)]
    status, _, consumed = await raw_call(app, "/relay/brain/ask", giant, bearer(login),
                                         declare=False)
    assert status == 413
    assert consumed < len(giant)


async def test_the_till_callback_keeps_its_small_limit(db):
    status, _, consumed = await raw_call(build(db, SECRET), "/billing/liqpay/callback",
                                         [b"x" * 70_000])
    assert status == 413 and consumed == 0


async def test_an_ordinary_small_request_is_untouched(db, login):
    app = build(db, SECRET)
    body = json.dumps({"email": "user@example.com", "password": PASSWORD}).encode()
    status, answer, _ = await raw_call(app, "/auth/sign-in", [body])
    assert status == 200 and json.loads(answer)["access"]


# --------------------------------------------------------------------------
# Memory budget for big bodies
# --------------------------------------------------------------------------


class Pause:
    """Fake provider: it does not answer right away, so the questions run concurrently."""

    def __init__(self, delay: float = 0.3, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.calls = 0

    async def __call__(self, req):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("the provider refused")
        return relay.BrainAskResponse(text="ok", input_tokens=1, output_tokens=1)


def heavy() -> bytes:
    """A question of about 1.2 MB: clearly larger than a "small" body, but under the limit."""
    return json.dumps(ask(system=blob(100_000), image_b64=blob(1_100_000))).encode()


async def test_big_questions_beyond_the_memory_budget_are_turned_away_before_parsing(
        db, login, monkeypatch):
    """Each large body stays in memory at several times its own size while the question runs:
    without accounting, a dozen parallel 20 MB requests from any registered user would exhaust
    the server's memory. The excess requests get "busy" right away, before parsing, and never
    reach the provider."""
    upstream = Pause()
    monkeypatch.setattr(relay, "_ask_openai", upstream)
    entitlements.credit(db, login.account_id, 5 * pricing.MICRO, "topup")
    body = heavy()
    monkeypatch.setattr(limits, "BIG_BUDGET", 2 * len(body) + 1000)       # room for exactly two
    app = build(db, SECRET)

    replies = await asyncio.gather(*[
        raw_call(app, "/relay/brain/ask", [body], bearer(login)) for _ in range(4)])

    assert sorted(status for status, _, _ in replies) == [200, 200, 503, 503]
    refused = next(answer for status, answer, _ in replies if status == 503)
    assert "busy" in json.loads(refused)["detail"]
    assert upstream.calls == 2, "a refused question reached the provider"
    status, _, _ = await raw_call(app, "/relay/brain/ask", [body], bearer(login))
    assert status == 200, "the budget did not come back when the questions were over"


async def test_the_busy_answer_says_when_to_come_back(db, login, monkeypatch):
    monkeypatch.setattr(limits, "BIG_BUDGET", 1)
    async with client(db) as http:
        r = await http.post("/relay/brain/ask", content=heavy(), headers={
            "Authorization": f"Bearer {login.access}", "Content-Type": "application/json"})
    assert r.status_code == 503
    assert int(r.headers["retry-after"]) > 0
    assert "busy" in r.json()["detail"]


async def test_small_bodies_never_spend_the_budget(db, login, monkeypatch):
    monkeypatch.setattr(limits, "BIG_BUDGET", 1)
    body = json.dumps({"email": "user@example.com", "password": PASSWORD}).encode()
    status, answer, _ = await raw_call(build(db, SECRET), "/auth/sign-in", [body])
    assert status == 200 and json.loads(answer)["access"]


async def test_the_budget_comes_back_even_when_the_question_fails(db, login, monkeypatch):
    monkeypatch.setattr(relay, "_ask_openai", Pause(delay=0.01, fail=True))
    entitlements.credit(db, login.account_id, 5 * pricing.MICRO, "topup")
    body = heavy()
    monkeypatch.setattr(limits, "BIG_BUDGET", len(body) + 1000)           # room for one question
    app = build(db, SECRET)
    for _ in range(3):
        status, _, _ = await raw_call(app, "/relay/brain/ask", [body], bearer(login))
        assert status == 502, "the budget did not come back after a failed question"


async def test_a_body_that_arrives_in_pieces_is_counted_as_it_arrives(
        db, login, monkeypatch):
    monkeypatch.setattr(relay, "_ask_openai", Pause())
    entitlements.credit(db, login.account_id, 5 * pricing.MICRO, "topup")
    body = heavy()
    pieces = [body[i:i + 200_000] for i in range(0, len(body), 200_000)]
    monkeypatch.setattr(limits, "BIG_BUDGET", len(body) + 1000)
    app = build(db, SECRET)
    replies = await asyncio.gather(*[
        raw_call(app, "/relay/brain/ask", pieces, bearer(login), declare=False)
        for _ in range(2)])
    assert sorted(status for status, _, _ in replies) == [200, 503]


# --------------------------------------------------------------------------
# Question fields
# --------------------------------------------------------------------------


def client(db) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build(db, SECRET)),
                             base_url="http://server", timeout=60.0)


@pytest.mark.parametrize("over, words", [
    ({"system": blob(relay.MAX_SYSTEM_CHARS + 1)}, "materials"),
    ({"prompt": blob(relay.MAX_PROMPT_CHARS + 1)}, "materials"),
    ({"image_b64": blob(relay.MAX_IMAGE_B64 + 1)}, "images"),
    ({"pinned_b64": [blob(10)] * (relay.MAX_IMAGES + 1)}, "images"),
    ({"pinned_b64": [blob(relay.MAX_IMAGE_B64 + 1)]}, "images"),
    ({"max_tokens": relay.MAX_ANSWER_TOKENS + 1}, "too long"),
])
async def test_what_does_not_fit_is_refused_in_words_a_person_can_read(db, login, over, words):
    async with client(db) as http:
        r = await http.post("/relay/brain/ask", json=ask(**over),
                            headers={"Authorization": f"Bearer {login.access}"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, str), "the app does not show a list of errors"
    assert words in detail


async def test_the_limits_themselves_pass(db, login, monkeypatch):
    seen = {}

    async def fake(req):
        seen.update(system=len(req.system), prompt=len(req.prompt), pages=len(req.pinned_b64),
                    tokens=req.max_tokens)
        return relay.BrainAskResponse(text="ok", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(relay, "_ask_openai", fake)
    entitlements.credit(db, login.account_id, 20 * pricing.MICRO, "topup")
    body = ask(system=blob(relay.MAX_SYSTEM_CHARS), prompt=blob(relay.MAX_PROMPT_CHARS),
               pinned_b64=[blob(1_000)] * relay.MAX_IMAGES, max_tokens=relay.MAX_ANSWER_TOKENS)
    async with client(db) as http:
        r = await http.post("/relay/brain/ask", json=body,
                            headers={"Authorization": f"Bearer {login.access}"})
    assert r.status_code == 200, r.text
    assert seen == {"system": relay.MAX_SYSTEM_CHARS, "prompt": relay.MAX_PROMPT_CHARS,
                    "pages": relay.MAX_IMAGES, "tokens": relay.MAX_ANSWER_TOKENS}


# --------------------------------------------------------------------------
# The biggest request the app builds
# --------------------------------------------------------------------------


def test_the_server_caps_cover_what_the_app_is_set_up_to_send():
    """The app's numbers and the server's numbers cannot be changed separately: raise the
    materials limit in settings, and a question with materials starts failing for everyone
    who has it turned on."""
    materials = Materials()
    brain = Config().brain
    # Materials go into the system part together with the main prompt and the profile: we take
    # the system part the way the app builds it (`assist.prompt.compose`), with materials up to
    # the limit.
    system = compose(AssistSettings(), materials="x" * materials.max_chars, has_image=True)
    assert relay.MAX_SYSTEM_CHARS >= len(system) + 20_000
    # The timeline and the question go in `prompt`; the screen frame is one more picture
    # alongside the materials pages.
    assert relay.MAX_PROMPT_CHARS >= brain.max_context_chars + 20_000
    assert relay.MAX_IMAGES >= materials.max_pages + 1
    assert relay.MAX_ANSWER_TOKENS >= brain.max_tokens


async def test_the_largest_question_the_app_builds_reaches_the_model(db, login, monkeypatch):
    """Default materials up to the limit, the timeline and the question, 40 pages and a screen
    frame.
    """
    materials = Materials()
    brain = Config().brain
    seen = {}

    async def fake(req):
        seen.update(system=req.system, pages=len(req.pinned_b64), screen=bool(req.image_b64))
        return relay.BrainAskResponse(text="answer", input_tokens=200_000, output_tokens=500)

    monkeypatch.setattr(relay, "_ask_openai", fake)
    page = blob(300_000)                    # a JPEG page with a long side of 1568 is hundreds of KB
    body = ask(
        system=compose(AssistSettings(), materials="x" * materials.max_chars, has_image=True),
        prompt="q" * (brain.max_context_chars + 3_000),
        pinned_b64=[page] * materials.max_pages,
        image_b64=page,
        max_tokens=brain.max_tokens,
        effort=brain.effort,
    )
    async with client(db) as http:
        r = await http.post("/relay/brain/ask", json=body,
                            headers={"Authorization": f"Bearer {login.access}"})
    assert r.status_code == 200, r.text
    assert seen["pages"] == materials.max_pages and seen["screen"]
    assert len(seen["system"]) >= materials.max_chars


async def test_an_expired_pass_gets_a_401_to_refresh_not_a_too_big_refusal(db):
    """A person with an expired pass sends an honest question with materials: they need "refresh
    your pass" (401), not "request too large"."""
    from argus.server import tokens

    expired = tokens.sign_access(SECRET, 1, ttl=-10)
    big = json.dumps(ask(system=blob(150_000), image_b64=blob(3_000_000))).encode()
    status, answer, _ = await raw_call(
        build(db, SECRET), "/relay/brain/ask", [big],
        [(b"authorization", f"Bearer {expired}".encode())])
    assert status == 401, answer


async def test_uploads_stalled_half_way_cannot_hold_more_memory_than_the_budget(db, login, monkeypatch):
    """Fifty uploads that hung before reaching the end used to cost no budget, and together they
    would have held more memory than the container has. Now the budget is taken as the bytes
    arrive."""
    body = heavy()
    pieces = [body[i:i + 200_000] for i in range(0, len(body), 200_000)][:-1]     # the last one will not get through
    monkeypatch.setattr(limits, "BIG_BUDGET", 2 * len(body))                       # room for two questions
    app = build(db, SECRET)

    async def stalled():
        sent = 0
        status = 0

        async def receive():
            nonlocal sent
            if sent < len(pieces):
                sent += 1
                return {"type": "http.request", "body": pieces[sent - 1], "more_body": True}
            await asyncio.Event().wait()                                           # silence after that

        async def send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "path": "/relay/brain/ask", "raw_path": b"/relay/brain/ask", "query_string": b"",
                 "root_path": "", "scheme": "http", "server": ("server", 80), "client": ("198.51.100.1", 5000),
                 "headers": [(b"content-type", b"application/json"), *bearer(login)]}
        task = asyncio.ensure_future(app(scope, receive, send))
        await asyncio.wait({task}, timeout=1.5)
        stuck = not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return status, stuck

    results = await asyncio.gather(*[stalled() for _ in range(6)])
    refused = [status for status, _ in results if status == 503]
    holding = [stuck for _, stuck in results if stuck]
    assert refused, "the stalled uploads did not run into the budget"
    assert len(holding) <= 2, "more stalled uploads are held than fit into the budget"
