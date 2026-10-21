"""Terms and policy: building the pages and what they promise.

The documents are written once in English and built into two copies, the download page
and the application itself. What is checked is the build (operator substitution, marks,
release mode) and what cannot be noticed by eye: a promise in the text that is no longer
in the application (a "Delete account" button with no such button).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from argus.account.legal import DOCS, TERMS_VERSION

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "argus" / "ui" / "static"

OPERATOR = {"name": "Acme Labs", "email": "legal@acme.example",
            "country": "Freedonia", "law": "the law of Freedonia"}


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location("legal_build", ROOT / "legal" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def test_both_documents_are_built_into_every_folder(builder, tmp_path):
    a, b = tmp_path / "site", tmp_path / "app"
    assert builder.build(((a, ""), (b, "legal-")), OPERATOR) == set()
    for doc in DOCS:
        assert (a / f"{doc}.html").is_file()
        assert (b / f"legal-{doc}.html").is_file()


def test_the_operator_is_filled_in_and_nothing_is_left_over(builder, tmp_path):
    builder.build(((tmp_path, ""),), OPERATOR)
    for doc in DOCS:
        page = (tmp_path / f"{doc}.html").read_text(encoding="utf-8")
        assert "{{" not in page and "}}" not in page
        assert "Acme Labs" in page
        assert 'href="mailto:legal@acme.example"' in page
        assert 'class="todo"' not in page


def test_the_version_date_is_in_the_text(builder, tmp_path):
    builder.build(((tmp_path, ""),), OPERATOR, version="2026-10-06")
    assert "October 6, 2026" in (tmp_path / "terms.html").read_text(encoding="utf-8")


def test_an_empty_field_stays_visible_as_a_mark(builder, tmp_path):
    missing = builder.build(((tmp_path, ""),), {})
    assert missing == {"name", "email", "law"}      # the country is not mentioned in the texts (decision of 2026-10-15)
    page = (tmp_path / "terms.html").read_text(encoding="utf-8")
    assert 'class="todo"' in page
    # There is no markup in a `mailto:` address: it would break the attribute.
    assert 'href="mailto:TODO-email"' in page


def test_release_refuses_to_publish_with_gaps(builder, tmp_path):
    assert builder.run({}, ((tmp_path, ""),), release=True) == 1
    assert builder.run({}, ((tmp_path, ""),), release=False) == 0
    assert builder.run(OPERATOR, ((tmp_path, ""),), release=True) == 0


def test_a_value_cannot_inject_markup(builder, tmp_path):
    builder.build(((tmp_path, ""),), {**OPERATOR, "name": "<script>alert(1)</script>"})
    page = (tmp_path / "terms.html").read_text(encoding="utf-8")
    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;" in page


def test_the_page_can_hide_its_site_chrome_inside_the_app(builder, tmp_path):
    builder.build(((tmp_path, ""),), OPERATOR)
    page = (tmp_path / "terms.html").read_text(encoding="utf-8")
    assert "embed=1" in page and "html.embed .top" in page


# --------------------------------------------------------------------------
# What the text promises and what the application has
# --------------------------------------------------------------------------


def text_of(doc: str) -> str:
    return (ROOT / "legal" / "text" / f"{doc}.html").read_text(encoding="utf-8")


def test_the_policy_names_only_the_buttons_the_app_really_has():
    # The policy must not refer to a button that the application does not have, and vice
    # versa: if the button exists, the text names it. Button names are the interface strings
    # themselves (English is the source language). The "Data" block ("Download my data",
    # "Delete account") was removed on 2026-10-08, so the
    # policy refers to an email to the operator's address; if the buttons come back, the test
    # will require the text about them to come back too.
    app = (STATIC / "account.js").read_text(encoding="utf-8")
    for button in ("Download my data", "Delete account"):
        assert (button in text_of("privacy")) == (button in app), button
    assert "write to us" in text_of("privacy")


def test_the_documents_are_in_english_only():
    for doc in DOCS:
        assert not re.search("[\u0400-\u04ff]", text_of(doc)), doc


def test_the_terms_link_to_the_policy_and_back(builder, tmp_path):
    builder.build(((tmp_path, ""),), OPERATOR)
    assert 'href="privacy.html"' in (tmp_path / "terms.html").read_text(encoding="utf-8")
    assert 'href="terms.html"' in (tmp_path / "privacy.html").read_text(encoding="utf-8")


def test_the_terms_describe_the_balance_the_app_sells():
    # There is no subscription, and the minimum top-up is $5; the terms must not lag behind
    # the prices.
    from argus.server import pricing

    terms = text_of("terms")
    assert "no subscription" in terms.lower()
    assert f"{pricing.SYMBOL}{pricing.MIN_TOPUP}" in terms


def test_the_copies_on_disk_are_what_the_build_gives_now(builder, tmp_path):
    # The copies (the download page and the one inside the application) are built by
    # `legal/build.py`. An edit of `legal/text/*`, of the version or of `operator.json`
    # without a rebuild left both copies old and equal to each other; the earlier test
    # compared them with each other and said nothing. Here they are compared with a fresh
    # build from the same sources.
    operator = json.loads((ROOT / "legal" / "operator.json").read_text(encoding="utf-8"))
    fresh = tmp_path / "fresh"
    builder.build(((fresh, ""),), operator)           # the version is `TERMS_VERSION`
    for doc in DOCS:
        want = (fresh / f"{doc}.html").read_text(encoding="utf-8")
        site = (ROOT / "deploy" / "server" / "downloads" / f"{doc}.html").read_text(encoding="utf-8")
        copy = (STATIC / f"legal-{doc}.html").read_text(encoding="utf-8")
        assert site == want, f"{doc}: the download page is out of date; run python legal/build.py"
        assert copy == want, f"{doc}: the copy in the app is out of date; run python legal/build.py"
        assert builder.human_date(TERMS_VERSION) in want


def test_the_terms_do_not_send_people_to_a_button_the_app_does_not_have():
    # "Delete the account in the account section" with no such button sends a person to an
    # empty place: the terms say the same as the policy (section 6), which is to write to the
    # operator's address. If the button comes back, the text will have to come back with it.
    app = (STATIC / "account.js").read_text(encoding="utf-8")
    terms = text_of("terms")
    if "Delete account" not in app:
        assert "account section" not in terms.lower()
        assert "ask us to delete your account" in terms
    for part in terms.split("ask us to delete your account")[1:]:
        assert "{{email}}" in part[:160], "the deletion request has no address to which it can be sent"


def test_documents_do_not_promise_a_notice_the_app_no_longer_shows():
    # The "I accept" banner is no longer in the application (2026-10-08), and
    # raising the version notifies nobody: we announce changes by email and publish the new
    # text at the same address, which is what the documents say.
    for doc in DOCS:
        text = text_of(doc)
        assert "material changes in the app" not in text, doc
        assert "email you about material changes" in text, doc
        assert "same address" in text, doc


def test_documents_describe_the_macos_build_too():
    from argus import host
    from argus.update_service import CHECK_EVERY_S

    terms, privacy = text_of("terms"), text_of("privacy")
    assert "Windows and macOS" in terms and "macOS 15" in terms
    assert f"Library/Application Support/{host.APP_NAME}" in privacy       # data on Mac is not next to the program
    assert "Trash does not remove" in privacy
    # Update check: at startup (`maybe_self_update`) and then every 30 minutes; the request
    # carries no version (`update.fetch_latest` is a plain GET with no parameters).
    assert "when it starts and then every" in privacy and f"every {int(CHECK_EVERY_S // 60)} minutes" in privacy
    assert "your version number" not in privacy and "no version number" in privacy


# Which version (`TERMS_VERSION`) was released with which text. The documents are signed
# with a version-date, and an edit of the text without a new date left "the version of
# 6 October" above the text of the 14th (audit of 2026-10-14, docs-legal-7). If you edit
# `legal/text/*`, the test fails; decide: if the meaning changed, raise `TERMS_VERSION` in
# `account/legal.py`, otherwise (a typo) keep the version; in both cases write the new hash
# here, and when the version changes, write it too.
PINNED_VERSION = "2026-10-14"
# 2026-10-15: the operator's country was removed from the English text ("The data
# controller is X, Country" -> "...is X"); the version was not raised: it takes nothing
# away from people, and the rights and terms are the same (decision: personal data and the
# country are not written anywhere).
PINNED_SHA256 = "9b9e6ac7556892450f22b71f7d847fab0de18c658c5dec0fec945c3e11826651"


def test_the_text_changed_only_together_with_the_version():
    digest = hashlib.sha256(b"".join(text_of(doc).encode("utf-8") for doc in DOCS)).hexdigest()
    if digest != PINNED_SHA256 and TERMS_VERSION == PINNED_VERSION:
        pytest.fail("the text of the terms or the policy changed but the version is the same: raise TERMS_VERSION (account/legal.py) "
                    "if the meaning changed, and write the new digest into PINNED_* of this test: " + digest)
    assert (TERMS_VERSION, digest) == (PINNED_VERSION, PINNED_SHA256), (
        "the version or the text changed: write them into PINNED_VERSION and PINNED_SHA256 of this test (digest: " + digest + ")")


# ---------------------------------------------------------------- links inside pages

NODE = shutil.which("node")


def _links_after_script(page: str, pathname: str, search: str) -> dict[str, str]:
    """Run both of the page's built-in scripts in `node` with a stub `document` and return the link
    addresses.
    """
    scripts = re.findall(r"<script>(.*?)</script>", page, flags=re.DOTALL)
    harness = r"""
const hrefs = JSON.parse(process.env.HREFS);
const links = hrefs.map((h) => ({ h, getAttribute() { return this.h; }, setAttribute(k, v) { this.h = v; } }));
globalThis.location = { pathname: process.env.PATHNAME, search: process.env.SEARCH };
globalThis.document = {
  documentElement: { className: "" },
  querySelectorAll(sel) {
    if (sel === "a[href$='.html']") return links.filter((l) => l.h.endsWith(".html"));
    if (sel === "a[href='./']") return links.filter((l) => l.h === "./");
    return [];
  },
};
for (const code of JSON.parse(process.env.SCRIPTS)) new Function(code)();
console.log(JSON.stringify(Object.fromEntries(links.map((l, i) => [hrefs[i], l.h]))));
"""
    env = {**os.environ, "HREFS": json.dumps(["privacy.html", "terms.html", "./"]), "PATHNAME": pathname,
           "SEARCH": search, "SCRIPTS": json.dumps(scripts)}
    out = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, env=env, timeout=60, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node is needed to run the page script")
def test_links_between_the_documents_work_in_a_tab_and_in_the_window_and_on_the_site(builder, tmp_path):
    builder.build(((tmp_path, ""),), OPERATOR)
    page = (tmp_path / "terms.html").read_text(encoding="utf-8")
    # In the application window: the neighboring document lies under a different name, and the
    # address carries ?embed=1.
    assert _links_after_script(page, "/p/legal-terms", "?embed=1") == {
        "privacy.html": "legal-privacy?embed=1", "terms.html": "legal-terms?embed=1", "./": "/"}
    # In an ordinary tab (the link from the account is a real one): `/p/privacy.html` and `/p/`
    # would give 404.
    assert _links_after_script(page, "/p/legal-terms", "") == {
        "privacy.html": "legal-privacy", "terms.html": "legal-terms", "./": "/"}
    # On the site next to the download page the links stay as they are.
    assert _links_after_script(page, "/terms.html", "") == {
        "privacy.html": "privacy.html", "terms.html": "terms.html", "./": "./"}
