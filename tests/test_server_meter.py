"""Recognition metering: the client reports seconds, the server charges money.

The audio goes from the key on the person's computer, and the server knows about it only
from the report. What is checked is what is required of the server with such a design:
charge honestly, reject obvious garbage, and answer whether it is possible to continue.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from argus.server import entitlements, pricing
from argus.server.app import MAX_STT_REPORT_S, build
from argus.server.db import connect

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "a-fairly-long-password"


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "accounts.sqlite")


@pytest.fixture
def http(db):
    with TestClient(build(db, SECRET)) as client:
        yield client


def _access(http) -> dict:
    body = http.post(
        "/auth/register",
        json={"email": "user@example.com", "password": PASSWORD, "terms": "t"},
    ).json()
    return {"Authorization": f"Bearer {body['access']}"}


def test_a_report_needs_a_pass(http):
    assert http.post("/meter/stt", json={"seconds": 10}).status_code == 401


def test_a_report_charges_the_balance(http, db):
    headers = _access(http)
    before = entitlements.balance(db, 1)

    r = http.post("/meter/stt", json={"seconds": 60}, headers=headers)
    assert r.status_code == 200

    take = pricing.charged(pricing.stt_cost(60))
    assert entitlements.balance(db, 1) == before - take
    assert r.json()["right"]["balance_micro"] == before - take
    assert entitlements.spent(db, 1, "stt_seconds") == 60


def test_the_answer_says_when_to_stop(http, db):
    """The client stops recognition by `allowed`, not by its own count."""
    headers = _access(http)
    entitlements.credit(db, 1, 5 * pricing.MICRO, "topup")
    db.execute("UPDATE accounts SET balance_micro = ?", (pricing.charged(pricing.stt_cost(30)),))
    db.commit()

    first = http.post("/meter/stt", json={"seconds": 30}, headers=headers).json()
    assert first["right"]["allowed"] is False
    assert "top up" in first["right"]["reason"]


@pytest.mark.parametrize("seconds", [0, -5, MAX_STT_REPORT_S + 1, 86_400])
def test_a_nonsense_report_is_refused(http, db, seconds):
    """A single bad number must not be able to charge the person everything."""
    headers = _access(http)
    before = entitlements.balance(db, 1)
    r = http.post("/meter/stt", json={"seconds": seconds}, headers=headers)
    assert r.status_code == 422
    assert entitlements.balance(db, 1) == before


def test_the_longest_allowed_report_passes(http):
    headers = _access(http)
    assert http.post("/meter/stt", json={"seconds": MAX_STT_REPORT_S},
                     headers=headers).status_code == 200


def test_an_hour_of_recognition_costs_what_the_price_list_says(http, db):
    headers = _access(http)
    entitlements.credit(db, 1, 5 * pricing.MICRO, "topup")
    before = entitlements.balance(db, 1)

    for _ in range(3600 // 60):
        http.post("/meter/stt", json={"seconds": 60}, headers=headers)

    spent = before - entitlements.balance(db, 1)
    assert spent == pytest.approx(pricing.charged(pricing.stt_cost(3600)), rel=0.01)


def test_the_sharper_tier_costs_what_the_price_list_says(http, db):
    headers = _access(http)
    before = entitlements.balance(db, 1)

    r = http.post("/meter/stt", json={"seconds": 60, "tier": "high"}, headers=headers)
    assert r.status_code == 200

    take = pricing.charged(pricing.stt_cost(60, "high"))
    assert take > pricing.charged(pricing.stt_cost(60))
    assert entitlements.balance(db, 1) == before - take


def test_a_report_without_a_tier_is_the_plain_one(http, db):
    """The old app does not send the field, and pays the plain price, as before."""
    headers = _access(http)
    before = entitlements.balance(db, 1)
    http.post("/meter/stt", json={"seconds": 60}, headers=headers)
    assert entitlements.balance(db, 1) == before - pricing.charged(pricing.stt_cost(60))


def test_an_unknown_tier_is_refused(http, db):
    """A bad value must not quietly cost the same as the plain tier, or as the expensive one."""
    headers = _access(http)
    before = entitlements.balance(db, 1)
    r = http.post("/meter/stt", json={"seconds": 60, "tier": "ultra"}, headers=headers)
    assert r.status_code == 422
    assert entitlements.balance(db, 1) == before


# --------------------------------------------------------------------------
# Report id: a repeat of the same report is not charged a second time
# --------------------------------------------------------------------------


def test_a_repeated_report_id_is_charged_once(http, db):
    """The response got lost, so the client sends the same report again. The second time nothing
    is charged.
    """
    headers = _access(http)
    before = entitlements.balance(db, 1)
    body = {"seconds": 60, "report_id": "chunk-0001-abcdef"}

    first = http.post("/meter/stt", json=body, headers=headers)
    again = http.post("/meter/stt", json=body, headers=headers)

    assert first.status_code == again.status_code == 200
    take = pricing.charged(pricing.stt_cost(60))
    assert entitlements.balance(db, 1) == before - take                    # and not before - 2*take
    assert again.json()["right"]["balance_micro"] == before - take         # the same answer
    assert again.json() == first.json()
    assert db.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 1
    assert entitlements.spent(db, 1, "stt_seconds") == 60


def test_different_report_ids_are_charged_separately(http, db):
    headers = _access(http)
    before = entitlements.balance(db, 1)
    for i in range(3):
        assert http.post("/meter/stt", json={"seconds": 60, "report_id": f"report-{i:04d}"},
                         headers=headers).status_code == 200
    assert entitlements.balance(db, 1) == before - 3 * pricing.charged(pricing.stt_cost(60))


def test_reports_without_a_number_are_all_charged_as_before(http, db):
    """The old app sends no number, so every report is separate, as always."""
    headers = _access(http)
    before = entitlements.balance(db, 1)
    for _ in range(2):
        http.post("/meter/stt", json={"seconds": 60}, headers=headers)
    assert entitlements.balance(db, 1) == before - 2 * pricing.charged(pricing.stt_cost(60))
    assert db.execute("SELECT COUNT(*) FROM usage WHERE report_id IS NULL").fetchone()[0] == 2


def test_a_number_is_scoped_to_the_account(http, db):
    """Someone else's number is no reason not to charge me: numbers are unique within an account."""
    mine = _access(http)
    other = http.post("/auth/register", json={"email": "friend@example.com", "password": PASSWORD,
                                                "terms": "t"}).json()
    theirs = {"Authorization": f"Bearer {other['access']}"}
    body = {"seconds": 30, "report_id": "same-number-00"}
    before_mine, before_theirs = entitlements.balance(db, 1), entitlements.balance(db, 2)

    http.post("/meter/stt", json=body, headers=mine)
    http.post("/meter/stt", json=body, headers=theirs)

    take = pricing.charged(pricing.stt_cost(30))
    assert entitlements.balance(db, 1) == before_mine - take
    assert entitlements.balance(db, 2) == before_theirs - take


@pytest.mark.parametrize("report_id", ["", "short", "x" * 65, "naïve-report-1234", "has space 1234",
                                       "semi;colon-1234", "../../etc/passwd"])
def test_a_malformed_report_id_is_refused(http, db, report_id):
    headers = _access(http)
    before = entitlements.balance(db, 1)
    r = http.post("/meter/stt", json={"seconds": 60, "report_id": report_id}, headers=headers)
    assert r.status_code == 422
    assert entitlements.balance(db, 1) == before


@pytest.mark.parametrize("report_id", ["a" * 8, "A" * 64, "mix_ed-ID_0123456789"])
def test_the_allowed_report_id_shapes_pass(http, report_id):
    headers = _access(http)
    assert http.post("/meter/stt", json={"seconds": 5, "report_id": report_id},
                     headers=headers).status_code == 200


def test_a_repeat_does_not_hide_a_nonsense_seconds_check(http, db):
    """The report size check runs before everything else: a number does not make a nonsense
    figure acceptable.
    """
    headers = _access(http)
    r = http.post("/meter/stt", json={"seconds": MAX_STT_REPORT_S + 1, "report_id": "chunk-0002-abcdef"},
                  headers=headers)
    assert r.status_code == 422
    assert db.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0


def test_the_database_itself_refuses_a_second_row_with_the_same_number(http, db):
    """A safety net under the check in code: a unique index, in case of a second process."""
    import sqlite3

    _access(http)
    row = "INSERT INTO usage (account_id, kind, amount, at, report_id) VALUES (1, 'stt_seconds', 1, 0, ?)"
    db.execute(row, ("dup-0000-number",))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(row, ("dup-0000-number",))
    db.rollback()
    db.execute(row.replace("?", "NULL"))                 # and empty ids do not get in each other's way
    db.execute(row.replace("?", "NULL"))
    db.commit()


def test_a_race_lost_to_the_index_is_a_repeat_not_a_crash(db, monkeypatch):
    """Two processes wrote the same number at once: the loser gets "already recorded", not a 500."""
    from argus.server import accounts

    login = accounts.register(db, SECRET, "race@example.com", PASSWORD)
    first = entitlements.charge(db, login.account_id, "stt_seconds", 5, 100, report_id="race-0000-number")
    assert first == pricing.charged(100)
    # The check in the code "did not see" the record (as if it had been made between the check
    # and the insert):
    real = db.execute
    calls = {"n": 0}

    class Blind:
        def __getattr__(self, name):
            return getattr(db, name)

        def execute(self, sql, *args):
            if sql.lstrip().startswith("SELECT 1 FROM usage"):
                calls["n"] += 1

                class Nothing:
                    @staticmethod
                    def fetchone():
                        return None
                return Nothing
            return real(sql, *args)

    before = entitlements.balance(db, login.account_id)
    assert entitlements.charge(Blind(), login.account_id, "stt_seconds", 5, 100,
                               report_id="race-0000-number") == 0
    assert calls["n"] == 1
    assert entitlements.balance(db, login.account_id) == before
