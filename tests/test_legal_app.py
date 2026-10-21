"""The application side: the recording notice, links to documents, consent.

What is checked is what breaks silently: recognition that switched on past the notice;
a link that can be substituted from the page; a mark that does not survive a restart.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from argus.account import legal
from argus.account.legal import Acks
from argus.app import Argus
from argus.core.config import Config
from argus.ui.server import build


@pytest.fixture
def made(tmp_path, monkeypatch):
    """The app without hardware; turning recognition on is recorded, not carried out."""
    cfg = Config()
    cfg.root = tmp_path
    app = Argus(cfg, with_screen=False, with_brain=False)
    calls: list[bool] = []
    monkeypatch.setattr(app, "set_running", lambda on: calls.append(bool(on)))
    return app, TestClient(build(app)), calls


# --------------------------------------------------------------------------
# The notice before the first switch-on
# --------------------------------------------------------------------------


def test_the_first_start_is_held_until_the_notice_is_read(made):
    _, client, calls = made
    out = client.post("/control", json={"running": True}).json()
    assert out["notice"] is True
    assert calls == []                       # recognition did not switch on


def test_after_the_notice_the_start_goes_through(made):
    _, client, calls = made
    client.post("/legal/notice")
    out = client.post("/control", json={"running": True}).json()
    assert "notice" not in out
    assert calls == [True]


def test_stopping_never_asks_for_the_notice(made):
    _, client, calls = made
    out = client.post("/control", json={"running": False}).json()
    assert "notice" not in out
    assert calls == [False]


def test_the_notice_can_be_switched_off_for_ones_own_build(made):
    app, client, calls = made
    app.cfg.legal.notice = False
    assert "notice" not in client.post("/control", json={"running": True}).json()
    assert calls == [True]


def test_a_refused_start_is_visible_in_the_snapshot(made):
    # It was switched on with a key combination: nobody returns an answer to the page, and it
    # learns that the notice must be shown from the state snapshot.
    _, client, _ = made
    assert client.get("/state").json()["notice_ask"] is False
    client.post("/control", json={"running": True})
    assert client.get("/state").json()["notice_ask"] is True
    client.post("/legal/notice")
    assert client.get("/state").json()["notice_ask"] is False


def test_the_confirmation_survives_a_restart(tmp_path):
    first = Acks(tmp_path)
    assert first.notice_done() is False
    first.confirm_notice()
    assert Acks(tmp_path).notice_done() is True


def test_an_old_notice_version_is_asked_again(tmp_path):
    (tmp_path / legal.STORE_NAME).write_text('{"notice": "2020-01-01"}', encoding="utf-8")
    assert Acks(tmp_path).notice_done() is False


def test_without_a_folder_the_confirmation_lives_in_memory():
    acks = Acks(None)
    acks.confirm_notice()
    assert acks.notice_done() is True


# --------------------------------------------------------------------------
# Settings in the shipped build
# --------------------------------------------------------------------------


def test_the_shipped_config_keeps_the_notice_on():
    from argus.core.config import find_config

    cfg = Config.load(find_config(), overrides=False)
    assert cfg.legal.notice is True


# --------------------------------------------------------------------------
# The account page: registration, data copy, deletion
# --------------------------------------------------------------------------

SECRET = "server-secret-long-enough-for-the-test"
PASSWORD = "quite-a-long-password"


@pytest.fixture
def with_server(tmp_path, monkeypatch):
    """The app with a real account server behind it, without the network."""
    from argus.server.app import build as build_server
    from argus.server.db import connect

    db = connect(tmp_path / "accounts.sqlite")
    server = TestClient(build_server(db, SECRET))

    def request(method, url, **kw):
        kw.pop("timeout", None)
        return server.request(method, url.replace("http://server", ""), **kw)

    monkeypatch.setattr("argus.account.client.httpx.request", request)
    monkeypatch.setattr("argus.account.client.httpx.post",
                        lambda url, **kw: request("POST", url, **kw))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / "Downloads").mkdir()

    cfg = Config()
    cfg.root = tmp_path
    cfg.ui.admin = True
    cfg.account.server = "http://server"
    app = Argus(cfg, with_screen=False, with_brain=False)
    return TestClient(build(app)), db, tmp_path


def test_the_account_state_carries_the_terms_version(with_server):
    client, _, _ = with_server
    assert client.get("/admin/account").json()["legal"]["terms"] == legal.TERMS_VERSION


def test_registering_from_the_page_records_the_accepted_version(with_server):
    client, db, _ = with_server
    out = client.post("/admin/account/register",
                      json={"email": "user@example.com", "password": PASSWORD}).json()
    assert out["ok"] is True
    assert db.execute("SELECT terms_version FROM accounts").fetchone()[0] == legal.TERMS_VERSION
    assert out["terms"] == legal.TERMS_VERSION


def test_the_page_cannot_choose_which_version_it_accepts(with_server):
    # The version that is accepted is the application's, the one the page showed, not what the
    # page sends in the request body.
    client, db, _ = with_server
    client.post("/admin/account/register",
                json={"email": "user@example.com", "password": PASSWORD, "terms": "1999-01-01"})
    assert db.execute("SELECT terms_version FROM accounts").fetchone()[0] == legal.TERMS_VERSION


def test_export_writes_a_file_into_downloads(with_server):
    import json

    client, _, home = with_server
    client.post("/admin/account/register", json={"email": "user@example.com", "password": PASSWORD})
    out = client.post("/admin/account/export", json={}).json()
    assert out["ok"] is True
    text = Path(out["path"]).read_text(encoding="utf-8")
    assert json.loads(text)["account"]["email"] == "user@example.com"
    assert str(home / "Downloads") in out["path"]
    assert "scrypt" not in text


def test_deleting_from_the_page_signs_out_and_removes_the_account(with_server):
    client, db, _ = with_server
    client.post("/admin/account/register", json={"email": "user@example.com", "password": PASSWORD})

    bad = client.post("/admin/account/delete", json={"password": "not-the-password"}).json()
    assert bad["ok"] is False
    assert client.get("/admin/account").json()["signed_in"] is True

    ok = client.post("/admin/account/delete", json={"password": PASSWORD}).json()
    assert ok["ok"] is True
    assert client.get("/admin/account").json()["signed_in"] is False
    assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_the_page_can_accept_the_terms_for_an_old_account(with_server):
    from argus.server import accounts as server_accounts

    client, db, _ = with_server
    server_accounts.register(db, SECRET, "old@example.com", PASSWORD)
    client.post("/admin/account/sign-in", json={"email": "old@example.com", "password": PASSWORD})
    assert client.get("/admin/account").json()["terms"] is None

    assert client.post("/admin/account/terms").json()["terms"] == legal.TERMS_VERSION


# --------------------------------------------------------------------------
# Documents inside the application
# --------------------------------------------------------------------------


def test_the_documents_are_served_by_the_apps_general_page_route(made):
    # The user pressed "Terms" in the application and reads in the application: the
    # text lies in the build and is served by the application itself, without going to the
    # network and without a browser. The route is a general one (`/p/<name>`), not a separate
    # one for documents: it exists in any build, and a document works as soon as the file is
    # there, without rebuilding the `.exe`.
    _, client, _ = made
    for name in ("terms", "privacy"):
        r = client.get(f"/p/legal-{name}")
        assert r.status_code == 200, name
        assert "text/html" in r.headers["content-type"]
        assert "Argus" in r.text


def test_the_app_links_point_at_pages_that_exist(made):
    # The links in the registration form (`account.js`, `docLink`) lead to `/p/legal-...`: if
    # the file is missing, the person sees `{"detail":"Not Found"}` instead of the document.
    import re

    _, client, _ = made
    static = Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "static"
    js = (static / "account.js").read_text(encoding="utf-8")
    links = {f"legal-{doc}" for doc in re.findall(r'docLink\("(terms|privacy)"', js)}
    assert links == {"legal-terms", "legal-privacy"}
    for link in links:
        assert client.get(f"/p/{link}").status_code == 200, link


def test_the_documents_are_linked_only_from_registration():
    # Requirement (2026-10-08): the terms and privacy policy links appear only at registration
    # and on the landing page, not everywhere. In the application the links stayed in one
    # place, the line under "Create account"; they are not in the notice
    # before the first switch-on, nor in the account ("In force ... I accept"), nor on other
    # pages.
    import re

    static = Path(__file__).resolve().parents[1] / "src" / "argus" / "ui" / "static"
    js = (static / "account.js").read_text(encoding="utf-8")
    assert len(re.findall(r"withDocs\(", js)) == 1  # the only call (the definition itself is `withDocs = (`)
    call = js[js.index("${up ? `<p class=\"ac-legal\">${withDocs("):]
    assert "By creating an account, you accept the {terms} and the {privacy}." in call[:200]
    assert "termsBanner" not in js and "ac-terms" not in js and "/admin/account/terms" not in js
    for page in static.glob("*.html"):
        if page.name.startswith("legal-"):
            continue
        html = page.read_text(encoding="utf-8")
        assert "data-doc" not in html and "/p/legal-" not in html and "showDoc" not in html, page.name
    assert "ac-terms" not in (static / "chrome.css").read_text(encoding="utf-8")


def test_inside_the_app_the_neighbour_link_stays_inside_the_app():
    # A link leads from the terms to the policy; in the application window it must lead to the
    # application page (`legal-privacy`), not to `privacy.html` next to the site.
    template = (Path(__file__).resolve().parents[1] / "legal" / "template.html").read_text(encoding="utf-8")
    assert '"legal-" + a.getAttribute("href")' in template


def test_the_old_routes_are_gone(made):
    _, client, _ = made
    assert client.post("/legal/open", json={"doc": "terms"}).status_code in (404, 405)
    assert client.get("/legal/doc/terms").status_code == 404
