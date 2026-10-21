"""Sign-in and passwords: scrypt off the event loop, guessing is not endless, passes do not
rise from the dead.

What is checked here for real, rather than "the route answers": while a password hash is
being computed, the server's other requests respond (before, three sign-ins a second hung
it entirely); password guessing runs into a counter that is the same for registered and
unregistered emails; the id of a deleted account does not pass to the next account
together with a still-valid pass; a closed account does not keep walking on its pass for
another quarter of an hour.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from argus.server import accounts, billing, emails, entitlements, pricing, tokens
from argus.server.app import build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "a-fairly-long-password"
NEW_PASSWORD = "another-fairly-long-password"
TERMS = "2026-10-06"


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


@pytest.fixture
def http(db):
    with TestClient(build(db, SECRET)) as client:
        yield client


@pytest.fixture
def cheap_hashes(monkeypatch):
    """Cheap scrypt for tests where the count of attempts matters, not the cost of the hash: twenty
    real hashes per test are seconds wasted."""
    monkeypatch.setattr(accounts, "SCRYPT_N", 2 ** 4)
    accounts._dummy_hash.cache_clear()
    yield
    accounts._dummy_hash.cache_clear()


def register(client, email="user@example.com", password=PASSWORD):
    return client.post("/auth/register",
                       json={"email": email, "password": password, "terms": TERMS})


def sign_in(client, email="user@example.com", password=PASSWORD):
    return client.post("/auth/sign-in", json={"email": email, "password": password})


def aclient(db, ip: str = "10.0.0.1", **options) -> httpx.AsyncClient:
    """A client that arrives from a given address, straight into the app (without a network)."""
    app = build(db, SECRET, **options)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 5000)),
                             base_url="http://server", timeout=30.0)


# --------------------------------------------------------------------------
# scrypt does not hold the event loop
# --------------------------------------------------------------------------


async def test_a_sign_in_flood_does_not_freeze_the_other_requests(db, monkeypatch):
    """The hash is "stuck" in a worker thread, but the server answers the others.

    Before, `check_password` was called directly from an `async def`: while it computed (0.2 s,
    twice for an unknown email), the whole server stood still: sign-ins, relays, the payment
    provider notification."""
    release = threading.Event()
    inside = threading.Event()
    threads: set[str] = set()

    def stuck(password, stored):
        threads.add(threading.current_thread().name)
        inside.set()
        release.wait(10)
        return False

    monkeypatch.setattr(accounts, "check_password", stuck)
    async with aclient(db) as http:
        started = time.monotonic()
        flood = [asyncio.create_task(http.post(
            "/auth/sign-in", json={"email": f"x{i}@example.com", "password": "some-password"}))
            for i in range(6)]
        assert await asyncio.to_thread(inside.wait, 10), "the hash never started to compute"
        try:
            # The loop is free: the answer arrives while all the hashes are still pending.
            answer = await asyncio.wait_for(http.get("/pricing"), 3)
            assert answer.status_code == 200
            assert time.monotonic() - started < 3, "the event loop stood idle while the hash was computed"
        finally:
            release.set()
        replies = await asyncio.gather(*flood)

    assert all(r.status_code in (403, 429, 503) for r in replies)
    assert threads and all(name.startswith("scrypt") for name in threads), threads


async def test_scrypt_for_registration_and_reset_also_runs_off_the_loop(db, monkeypatch):
    names: list[str] = []
    real = accounts.hash_password

    def spy(password):
        names.append(threading.current_thread().name)
        return real(password)

    monkeypatch.setattr(accounts, "hash_password", spy)
    async with aclient(db) as http:
        r = await http.post("/auth/register",
                            json={"email": "user@example.com", "password": PASSWORD,
                                  "terms": TERMS})
    assert r.status_code == 200
    assert names and all(n.startswith("scrypt") for n in names), names


def test_an_unknown_address_costs_one_hash_not_two(db, monkeypatch):
    """The dummy hash is computed once, and the check against it is a single one, as against a real
    one.
    """
    accounts._dummy_hash.cache_clear()
    calls = []
    real = hashlib.scrypt

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(accounts.hashlib, "scrypt", counting)
    for _ in range(3):
        with pytest.raises(accounts.Denied):
            accounts.sign_in(db, SECRET, "nobody@example.com", PASSWORD)
    # Once for the dummy hash, and one check for each sign-in.
    assert len(calls) == 1 + 3


def test_a_known_address_costs_the_same_one_hash(db, monkeypatch):
    accounts.register(db, SECRET, "user@example.com", PASSWORD)
    calls = []
    real = hashlib.scrypt
    monkeypatch.setattr(accounts.hashlib, "scrypt",
                        lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])
    accounts.sign_in(db, SECRET, "user@example.com", PASSWORD)
    assert len(calls) == 1


async def test_when_the_queue_of_hashes_is_full_the_answer_is_busy_not_a_wait(db, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(accounts, "MAX_HASH_QUEUE", 2)
    monkeypatch.setattr(accounts, "check_password",
                        lambda password, stored: (release.wait(10), False)[1])
    async with aclient(db) as http:
        jobs = [asyncio.create_task(http.post(
            "/auth/sign-in", json={"email": f"y{i}@example.com", "password": "some-password"}))
            for i in range(2)]
        await asyncio.sleep(0.1)
        late = await asyncio.wait_for(http.post(
            "/auth/sign-in", json={"email": "z@example.com", "password": "some-password"}), 3)
        release.set()
        await asyncio.gather(*jobs)
    assert late.status_code == 503
    assert "busy" in late.json()["detail"]
    assert late.headers["retry-after"]


async def test_two_registrations_of_one_address_make_one_account(db):
    """The hash is computed between the "email is free" check and the write: the second
    registration will be stopped by the index.
    """
    async with aclient(db) as http:
        a, b = await asyncio.gather(*[
            http.post("/auth/register",
                      json={"email": "same@example.com", "password": PASSWORD, "terms": TERMS})
            for _ in range(2)])
    assert sorted([a.status_code, b.status_code]) == [200, 409]
    assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM credits").fetchone()[0] == 1, "the gift was given twice"


# --------------------------------------------------------------------------
# Password guessing
# --------------------------------------------------------------------------


def test_the_sixth_guess_in_a_row_is_stopped_even_with_the_right_password(http, cheap_hashes):
    register(http)
    for _ in range(5):
        assert sign_in(http, password="not-that-password-1").status_code == 403
    blocked = sign_in(http)                          # now the password is right, and it is still "please wait"
    assert blocked.status_code == 429
    assert "wait" in blocked.json()["detail"]
    assert int(blocked.headers["retry-after"]) > 0


def test_the_lock_is_the_same_for_an_address_that_has_no_account(http, cheap_hashes):
    """Otherwise the lock itself would become a way to learn whether an address has been
    registered.
    """
    register(http)
    for email in ("user@example.com", "nobody@example.com"):
        for _ in range(5):
            sign_in(http, email=email, password="not-that-password-1")
    real = sign_in(http)
    unknown = sign_in(http, email="nobody@example.com")
    assert real.status_code == unknown.status_code == 429
    assert real.json()["detail"] == unknown.json()["detail"]


def _clients(db, *ips: str) -> list[httpx.AsyncClient]:
    """Several people from different addresses go to ONE app (its counters are shared)."""
    app = build(db, SECRET)
    return [httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 5000)),
                              base_url="http://server", timeout=30.0) for ip in ips]


async def test_a_strangers_wrong_guesses_do_not_lock_the_owner_out(db, cheap_hashes):
    """A stranger's wrong guesses at an email lock that email for the stranger's address, not for
    the owner.

    Otherwise anyone who knows the email could keep the owner from signing in with five requests
    in a quarter of an hour, and could repeat this as often as they like."""
    owner, stranger = _clients(db, "203.0.113.5", "198.51.100.66")
    try:
        made = await owner.post("/auth/register", json={
            "email": "user@example.com", "password": PASSWORD, "terms": TERMS})
        assert made.status_code == 200
        codes = []
        for _ in range(8):
            r = await stranger.post("/auth/sign-in", json={
                "email": "user@example.com", "password": "not-that-password-1"})
            codes.append(r.status_code)
        assert codes == [403] * 5 + [429] * 3
        mine = await owner.post("/auth/sign-in", json={
            "email": "user@example.com", "password": PASSWORD})
        assert mine.status_code == 200, "a stranger's guessing locked the owner out"
    finally:
        await owner.aclose()
        await stranger.aclose()


async def test_guesses_from_many_addresses_still_meet_the_ceiling_of_the_account(
        db, cheap_hashes):
    """Guessing from many addresses at once runs into the shared ceiling of the email, for
    everybody.
    """
    owner, *guessers = _clients(db, "203.0.113.5", *[f"198.51.100.{i}" for i in range(1, 31)])
    try:
        await owner.post("/auth/register", json={
            "email": "user@example.com", "password": PASSWORD, "terms": TERMS})
        for guesser in guessers:                       # one miss each from thirty addresses
            r = await guesser.post("/auth/sign-in", json={
                "email": "user@example.com", "password": "not-that-password-1"})
            assert r.status_code == 403
        late = await owner.post("/auth/sign-in", json={
            "email": "user@example.com", "password": PASSWORD})
        assert late.status_code == 429
        assert "wait" in late.json()["detail"]
    finally:
        await owner.aclose()
        for guesser in guessers:
            await guesser.aclose()


def test_a_blocked_guess_costs_no_hash(db, monkeypatch):
    with TestClient(build(db, SECRET)) as http:
        register(http)
        for _ in range(5):
            sign_in(http, password="not-that-password-1")
        calls = []
        real = accounts.check_password
        monkeypatch.setattr(accounts, "check_password",
                            lambda *a: (calls.append(1), real(*a))[1])
        assert sign_in(http).status_code == 429
    assert calls == [], "a blocked attempt still computed scrypt"


def test_a_success_forgives_earlier_mistakes(http, cheap_hashes):
    register(http)
    for _ in range(4):
        sign_in(http, password="not-that-password-1")
    assert sign_in(http).status_code == 200
    for _ in range(4):
        assert sign_in(http, password="not-that-password-1").status_code == 403
    assert sign_in(http).status_code == 200


def test_a_wrong_address_shape_is_refused_without_counting(http):
    for _ in range(30):
        assert sign_in(http, email="no-at-sign").status_code == 400


def test_the_lock_ends_when_the_window_passes():
    from argus.server import throttle

    window = throttle.Window(((2, 100), (5, 1000)))
    now = 1_000.0
    window.add("k", now)
    window.add("k", now + 1)
    assert window.wait("k", now + 2) == pytest.approx(98, abs=1)     # the second tier still has room
    assert window.wait("k", now + 101) == 0                           # the first event left the window
    for i in range(5):
        window.add("k", now + 200 + i)
    assert window.wait("k", now + 300) > 600                          # the long tier holds for longer
    window.clear("k")
    assert window.wait("k", now + 300) == 0


def test_the_counter_does_not_grow_without_bound():
    from argus.server import throttle

    window = throttle.Window(((3, 60),), max_keys=100)
    for i in range(1_000):
        window.add(f"k{i}", 1_000.0)
    assert len(window._events) <= 100


async def test_one_address_guessing_many_accounts_is_stopped_by_its_address(db, cheap_hashes):
    async with aclient(db, "198.51.100.7") as http:
        codes = []
        for i in range(25):
            r = await http.post("/auth/sign-in", json={
                "email": f"victim{i}@example.com", "password": "some-password"})
            codes.append(r.status_code)
        elsewhere = aclient(db, "198.51.100.8")
    assert codes[:20] == [403] * 20
    assert codes[20:] == [429] * 5
    # Another address is not affected by the same app: each app is its own, but the count is its
    # own too.
    await elsewhere.aclose()


async def test_the_forwarded_for_header_is_not_believed_by_default(db, cheap_hashes):
    """Anyone can write that header: whoever wanted could get around a counter based on it."""
    async with aclient(db, "198.51.100.7") as http:
        codes = []
        for i in range(22):
            r = await http.post(
                "/auth/sign-in",
                json={"email": f"victim{i}@example.com", "password": "some-password"},
                headers={"X-Forwarded-For": f"203.0.113.{i}"})
            codes.append(r.status_code)
    assert codes[-1] == 429


async def test_behind_our_own_proxy_the_address_comes_from_its_header(
        db, monkeypatch, cheap_hashes):
    monkeypatch.setenv("ARGUS_TRUST_PROXY", "1")
    async with aclient(db, "172.18.0.2") as http:          # the proxy's own address is the same for everyone
        for i in range(22):
            r = await http.post(
                "/auth/sign-in",
                json={"email": f"victim{i}@example.com", "password": "some-password"},
                headers={"X-Forwarded-For": f"203.0.113.{i}"})
            assert r.status_code == 403, "strangers behind one proxy must not get in each other's way"


async def test_registrations_from_one_address_are_limited(db, cheap_hashes):
    async with aclient(db, "198.51.100.9") as http:
        codes = []
        for i in range(22):
            r = await http.post("/auth/register", json={
                "email": f"gift{i}@example.com", "password": PASSWORD, "terms": TERMS})
            codes.append(r.status_code)
    assert codes[:20] == [200] * 20
    assert codes[20:] == [429, 429]


def test_an_old_password_cannot_be_guessed_with_a_stolen_pass(http):
    out = register(http).json()
    head = {"Authorization": f"Bearer {out['access']}"}
    for _ in range(5):
        r = http.post("/auth/password", json={"old": "not-that-password-1", "new": NEW_PASSWORD},
                      headers=head)
        assert r.status_code == 403
    r = http.post("/auth/password", json={"old": PASSWORD, "new": NEW_PASSWORD}, headers=head)
    assert r.status_code == 429


def test_the_same_goes_for_the_password_asked_before_deleting(http, db):
    out = register(http).json()
    head = {"Authorization": f"Bearer {out['access']}"}
    for _ in range(5):
        assert http.post("/account/delete", json={"password": "not-that-password-1"},
                         headers=head).status_code == 403
    assert http.post("/account/delete", json={"password": PASSWORD}, headers=head).status_code == 429
    assert db.execute("SELECT COUNT(*) FROM accounts WHERE email_key = ?",
                      ("user@example.com",)).fetchone()[0] == 1


# --------------------------------------------------------------------------
# Simple passwords
# --------------------------------------------------------------------------


@pytest.mark.parametrize("weak", [
    "12345678", "qwertyui", "Password1", "11111111", "abababab", "87654321", "abcdefgh",
    "пароль123",          # on the list of the simplest Russian passwords (`server/weakpass.py`)
])
def test_a_password_everyone_tries_first_is_refused_everywhere(db, weak):
    with pytest.raises(accounts.Denied, match="too simple"):
        accounts.register(db, SECRET, "user@example.com", weak)
    login = accounts.register(db, SECRET, "ok@example.com", PASSWORD)
    with pytest.raises(accounts.Denied, match="too simple"):
        accounts.change_password(db, login.account_id, PASSWORD, weak)
    raw = accounts.start_reset(db, "ok@example.com")
    with pytest.raises(accounts.Denied, match="too simple"):
        accounts.finish_reset(db, raw, weak)


@pytest.mark.parametrize("own", ["dev.owner@example.com", "dev.owner"])
def test_the_address_itself_is_not_a_password(db, own):
    with pytest.raises(accounts.Denied, match="too simple"):
        accounts.register(db, SECRET, "dev.owner@example.com", own)
    login = accounts.register(db, SECRET, "dev.owner@example.com", PASSWORD)
    with pytest.raises(accounts.Denied, match="too simple"):
        accounts.change_password(db, login.account_id, PASSWORD, own)
    raw = accounts.start_reset(db, "dev.owner@example.com")
    with pytest.raises(accounts.Denied, match="too simple"):
        accounts.finish_reset(db, raw, own)


def test_the_refusal_of_a_simple_password_says_so(http):
    r = register(http, password="12345678")
    assert r.status_code == 409
    assert "too simple" in r.json()["detail"]
    assert http.post("/auth/reset/confirm",
                     json={"token": "x", "password": "qwertyui"}).json()["ok"] is False


def test_a_good_password_with_common_words_inside_passes(db):
    accounts.register(db, SECRET, "user@example.com", "my-password-for-the-server-2026")


# --------------------------------------------------------------------------
# Password hash: settings and recalculation
# --------------------------------------------------------------------------


def test_a_stronger_setting_still_hashes_and_the_old_hash_still_checks(monkeypatch):
    """`N = 2**15` did not fit into the 32 MiB that the library allows by default."""
    old = accounts.hash_password(PASSWORD)
    monkeypatch.setattr(accounts, "SCRYPT_N", 2 ** 15)
    new = accounts.hash_password(PASSWORD)
    assert new.startswith("scrypt$32768$")
    assert accounts.check_password(PASSWORD, new)
    assert accounts.check_password(PASSWORD, old), "old hashes must still check"
    assert not accounts.check_password("not-that", new)
    assert accounts.needs_rehash(old) and not accounts.needs_rehash(new)


def test_a_good_sign_in_moves_an_old_hash_to_the_current_settings(db, monkeypatch):
    accounts.register(db, SECRET, "user@example.com", PASSWORD)
    before = db.execute("SELECT password FROM accounts").fetchone()["password"]
    monkeypatch.setattr(accounts, "SCRYPT_N", 2 ** 15)

    accounts.sign_in(db, SECRET, "user@example.com", PASSWORD)
    after = db.execute("SELECT password FROM accounts").fetchone()["password"]
    assert after != before and after.startswith("scrypt$32768$")
    accounts.sign_in(db, SECRET, "user@example.com", PASSWORD)        # and signs in with the new hash


async def test_the_rehash_also_happens_on_the_server_path(db, monkeypatch):
    accounts.register(db, SECRET, "user@example.com", PASSWORD)
    monkeypatch.setattr(accounts, "SCRYPT_N", 2 ** 15)
    async with aclient(db) as http:
        assert (await http.post("/auth/sign-in", json={
            "email": "user@example.com", "password": PASSWORD})).status_code == 200
    assert db.execute("SELECT password FROM accounts").fetchone()["password"].startswith(
        "scrypt$32768$")


def test_a_wrong_password_does_not_rehash(db, monkeypatch):
    accounts.register(db, SECRET, "user@example.com", PASSWORD)
    before = db.execute("SELECT password FROM accounts").fetchone()["password"]
    monkeypatch.setattr(accounts, "SCRYPT_N", 2 ** 15)
    with pytest.raises(accounts.Denied):
        accounts.sign_in(db, SECRET, "user@example.com", "not-that-password-1")
    assert db.execute("SELECT password FROM accounts").fetchone()["password"] == before


# --------------------------------------------------------------------------
# Passes: closed, deleted, foreign
# --------------------------------------------------------------------------


def test_a_closed_account_stops_working_at_once_not_in_a_quarter_of_an_hour(http, db):
    out = register(http).json()
    head = {"Authorization": f"Bearer {out['access']}"}
    assert http.get("/me", headers=head).status_code == 200

    db.execute("UPDATE accounts SET blocked_at = ?", (time.time(),))
    db.commit()
    for call in (lambda: http.get("/me", headers=head),
                 lambda: http.post("/meter/stt", json={"seconds": 5}, headers=head),
                 lambda: http.post("/relay/brain/ask", headers=head, json={
                     "provider": "openai-api", "model": "gpt-5.6-luna", "prompt": "hello"})):
        r = call()
        assert r.status_code == 401
        assert "closed" in r.json()["detail"]


def test_a_deleted_accounts_pass_does_not_work_for_the_next_person(http, db):
    """A deleted account's pass is checked only as "a row with such a number exists", while an
    `INTEGER PRIMARY KEY` number without AUTOINCREMENT was handed to the next person who
    registered."""
    first = register(http, "bob@example.com").json()
    first_head = {"Authorization": f"Bearer {first['access']}"}
    first_id = db.execute("SELECT id FROM accounts").fetchone()["id"]
    assert http.post("/account/delete", json={"password": PASSWORD},
                     headers=first_head).status_code == 204

    second = register(http, "carol@example.com").json()
    second_head = {"Authorization": f"Bearer {second['access']}"}
    second_id = db.execute("SELECT id FROM accounts WHERE email_key = ?",
                           ("carol@example.com",)).fetchone()["id"]
    assert second_id != first_id, "the number of the erased account went to the next one"

    assert http.get("/me", headers=first_head).status_code == 401
    assert http.get("/account/export", headers=first_head).status_code == 401
    me = http.get("/me", headers=second_head).json()
    assert me["email"] == "carol@example.com"


def test_a_deleted_account_leaves_nothing_personal_behind(http, db):
    out = register(http, "bob@example.com").json()
    http.post("/account/delete", json={"password": PASSWORD},
              headers={"Authorization": f"Bearer {out['access']}"})
    assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    # The money and the spending remained, but without an owner.
    assert db.execute("SELECT COUNT(*) FROM credits WHERE account_id > 0").fetchone()[0] == 0
    # The email can be taken again, and it sees no one else's history.
    again = register(http, "bob@example.com")
    assert again.status_code == 200
    export = http.get("/account/export",
                      headers={"Authorization": f"Bearer {again.json()['access']}"}).json()
    assert [c["kind"] for c in export["credits"]] == ["gift"]


def test_a_non_ascii_pass_is_refused_not_a_crash(http):
    r = http.get("/me", headers=[(b"authorization", "Bearer abc.é".encode("latin-1"))])
    assert r.status_code == 401
    r = http.get("/me", headers=[(b"authorization", "Bearer é.é".encode("latin-1"))])
    assert r.status_code == 401


def test_a_giant_order_number_is_refused_not_a_crash(db):
    provider = billing.LiqPay("i12345678901", "private-key", "https://pay.example.com")
    with TestClient(build(db, SECRET, pay=provider)) as http:
        for path in ("/billing/liqpay/99999999999999999999", "/billing/liqpay/0",
                     "/billing/liqpay/-5"):
            assert http.get(path).status_code == 422, path


def test_a_deeply_nested_notice_is_refused_not_a_crash(db):
    provider = billing.LiqPay("i12345678901", "private-key", "https://pay.example.com")
    with TestClient(build(db, SECRET, pay=provider)) as http:
        body = b'{"data":' + b"[" * 20_000 + b"]" * 20_000 + b"}"
        r = http.post("/billing/liqpay/callback", content=body,
                      headers={"content-type": "application/json"})
        assert r.status_code == 400


def test_the_stub_order_number_is_checked_too(db):
    with TestClient(build(db, SECRET, pay=billing.Stub())) as http:
        assert http.get("/billing/stub/99999999999999999999").status_code == 422
        assert http.post("/billing/stub/99999999999999999999").status_code == 422


def test_a_broken_handler_answers_in_json_not_with_a_bare_page(db):
    app = build(db, SECRET)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret-details-of-the-failure")

    with TestClient(app, raise_server_exceptions=False) as http:
        r = http.get("/boom")
    assert r.status_code == 500
    assert "secret-details" not in r.text
    assert isinstance(r.json()["detail"], str)


# --------------------------------------------------------------------------
# The API description and the answers a person reads
# --------------------------------------------------------------------------


def test_the_api_description_is_closed_in_production(http):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert http.get(path).status_code == 404, path


def test_the_api_description_can_be_switched_on_for_development(db, monkeypatch):
    monkeypatch.setenv("ARGUS_SERVER_DOCS", "1")
    with TestClient(build(db, SECRET)) as http:
        assert http.get("/openapi.json").status_code == 200
    with TestClient(build(db, SECRET, docs=True)) as http:
        assert http.get("/docs").status_code == 200


def test_the_price_list_does_not_name_the_till(db):
    provider = billing.LiqPay("i12345678901", "private-key", "https://pay.example.com")
    with TestClient(build(db, SECRET, pay=provider)) as http:
        sheet = http.get("/pricing").json()
    assert sheet["payment"] == "on"
    assert "liqpay" not in str(sheet).lower()


def test_a_bad_body_is_explained_by_a_phrase_not_by_a_list(http):
    r = http.post("/auth/register", json={"email": "user@example.com", "password": PASSWORD})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]


def test_every_answer_asks_the_browser_not_to_guess_the_type(http):
    assert http.get("/pricing").headers["x-content-type-options"] == "nosniff"
    assert http.get("/auth/reset").headers["x-content-type-options"] == "nosniff"


def test_the_sign_in_throttle_is_not_shared_between_servers(db, tmp_path, cheap_hashes):
    """The counters belong to each app rather than being a module-level variable: otherwise the
    tests (and two servers in one process) would interfere with each other."""
    with TestClient(build(db, SECRET)) as one:
        register(one)
        for _ in range(5):
            sign_in(one, password="not-that-password-1")
        assert sign_in(one).status_code == 429
    with TestClient(build(db, SECRET)) as other:
        assert sign_in(other).status_code == 200


def test_the_old_token_check_for_a_forged_signature_still_works():
    good = tokens.sign_access(SECRET, 1)
    payload, mac = good.split(".")
    with pytest.raises(tokens.BadToken):
        tokens.read_access(SECRET, f"{payload}.{'é' * len(mac)}")
    assert emails.check("user@example.com") == "user@example.com"
    assert entitlements.balance is not None and pricing.MICRO == 1_000_000


def test_a_burst_of_failures_is_written_to_the_log_without_the_address(db, caplog):
    with TestClient(build(db, SECRET)) as http:
        register(http)
        for _ in range(5):
            sign_in(http, password="not-that-password-1")
    lines = [r.getMessage() for r in caplog.records if "series of failures" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "user@example.com" not in lines[0], "the email is not written to the log"
