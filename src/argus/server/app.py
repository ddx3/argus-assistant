"""Accounts and balance server.

A separate program, not part of the app. It lives with us, not with the user, and that is
the whole point: everything that decides about money and access must be somewhere the
buyer cannot reach with a debugger.

The app never receives model keys, neither in a file nor in a response. Expensive calls go
through the relays of this server (`server/relay.py`), and it also charges the balance for
them; speech recognition works the same way (`server/relay_stt.py`). The client report
(`/meter/stt`) remains for a build that recognizes speech with its own key. It is
accounting, not a lock.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, suppress
import hashlib
import html
import json
import logging
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from argus.server import (
    accounts,
    billing,
    emails,
    entitlements,
    limits,
    mailer,
    mailevents,
    mailtemplate,
    mailtexts,
    pricing,
    relay,
    throttle,
    tokens,
)
from argus.server.db import connect

log = logging.getLogger(__name__)

# The stub's "payment" page. Deliberately ugly and with a warning: it cannot be mistaken
# for a real payment provider by accident, and anyone who sees it in production should
# understand at once that the server was started wrongly.
STUB_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Payment stub</title>
<style>
 body { background:#14161a; color:#dfe3ea; font:15px/1.6 system-ui,sans-serif;
        display:grid; place-items:center; min-height:100vh; margin:0; }
 .card { max-width:420px; padding:26px 28px; background:#1c1f25;
         border:1px solid #2a2f38; border-radius:12px; }
 h1 { font-size:17px; margin:0 0 10px; }
 .warn { background:rgba(208,162,21,.12); border-left:2px solid #d0a215;
         padding:10px 13px; border-radius:7px; color:#e8cf7a; font-size:13px; }
 button { margin-top:18px; padding:9px 16px; font:inherit; cursor:pointer;
          background:#4fb8d8; border:0; border-radius:8px; color:#10141a; }
 .done { margin-top:14px; color:#63c77a; }
</style></head><body>
<div class="card">
  <h1>Payment stub</h1>
  <div class="warn">No money is taken. The button puts it on the balance right away —
    this is the <code>ARGUS_BILLING=stub</code> mode for development.</div>
  <button id="pay">Pay order #{order}</button>
  <div class="done" id="done" hidden>Done. Go back to the app.</div>
</div>
<script>
document.getElementById("pay").onclick = async (e) => {
  e.target.disabled = true;
  await fetch("/billing/stub/{order}", { method: "POST" });
  document.getElementById("done").hidden = false;
};
</script></body></html>"""

# Redirect to LiqPay (`billing.LiqPay`). The person barely sees it: the form submits itself
# as soon as the page loads. The button is for the case when scripts are disabled.
LIQPAY_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Argus — payment</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body { background:#0a0c10; color:#f2f4f7; font:15px/1.6 system-ui,sans-serif;
        display:grid; place-items:center; min-height:100vh; margin:0; text-align:center; }
 button { margin-top:14px; padding:8px 14px; font:inherit; cursor:pointer;
          background:#1d2025; border:0; border-radius:6px; color:#9aa1ad; }
</style></head><body>
<form id="f" method="POST" action="{action}" accept-charset="utf-8">
  <input type="hidden" name="data" value="{data}">
  <input type="hidden" name="signature" value="{signature}">
  <div>Taking you to the payment page…</div>
  <noscript><button type="submit">Continue to payment</button></noscript>
</form>
<script>document.getElementById("f").submit();</script>
</body></html>"""

# Opening the mono payment failed (the provider did not answer or did not accept the order).
# Shown to the person in English and without details: the reasons are in the server log.
MONO_ERROR_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Argus — payment</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body { background:#0a0c10; color:#f2f4f7; font:15px/1.6 system-ui,sans-serif;
        display:grid; place-items:center; min-height:100vh; margin:0; }
 .card { max-width:380px; width:100%; padding:26px 28px; box-sizing:border-box;
         background:#101318; border-radius:12px; }
 h1 { font-size:17px; margin:0 0 12px; }
 p { margin:0 0 10px; }
 .dim { color:#9aa1ad; font-size:13px; }
</style></head><body>
<div class="card">
  <h1>Could not open the payment page</h1>
  <p>Nothing was charged. Please go back to the app and try again in a minute.</p>
  <p class="dim">If this keeps happening, write to support@arguspilot.com.</p>
</div>
</body></html>"""

# Where the payment provider returns the person after payment. This page does not credit
# the money; the provider's notification does. So it claims nothing: both the "go back"
# button after a bank refusal and an old link lead here.
DONE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Argus</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body { background:#0a0c10; color:#f2f4f7; font:15px/1.6 system-ui,sans-serif;
        display:grid; place-items:center; min-height:100vh; margin:0; }
 /* Flat look, like the app windows: a card in the window color (`--win`), no borders. */
 .card { max-width:380px; width:100%; padding:26px 28px; box-sizing:border-box;
         background:#101318; border-radius:12px; }
 h1 { font-size:17px; margin:0 0 12px; }
 p { margin:0 0 10px; }
 .dim { color:#9aa1ad; font-size:13px; }
</style></head><body>
<div class="card">
  <h1>Back to Argus</h1>
  <p>If the payment went through, your balance will update in a few seconds.
     You can close this tab and return to the app.</p>
  <p class="dim">If the balance has not changed after a few minutes, write to support@arguspilot.com.</p>
</div>
</body></html>"""

# The page from the recovery email. One field, one button: people arrive here by a link,
# not to set up anything else.
#
# The token is NOT put into the markup. It used to be inserted into the script through
# `json.dumps`, but that does not touch `</script>`: a link such as
# `/auth/reset?token=</script><img onerror=…>` closed the script too early and executed
# foreign markup on our address. Now the page is the same for everyone, and its own script
# reads the token from the address, from the part after "#". That part is not sent to the
# server, so its request log does not accumulate live tokens. Emails sent before this
# change (`?token=`) are still read.
RESET_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>New password — Argus</title>
<style>
 /* Flat look, like the app windows (ui/static/chrome.css, window tokens): an even dark
    background, a card in the window color (`--win`) and a plate button (`--btn` .055),
    with no borders and no colored accent; the field gets an edge only on focus.
    Requirement: no borders anywhere. */
 body { background:#0a0c10; color:#f2f4f7; font:15px/1.6 system-ui,sans-serif;
        display:grid; place-items:center; min-height:100vh; margin:0; }
 .card { max-width:380px; width:100%; padding:26px 28px; background:#101318;
         border-radius:12px; box-sizing:border-box; }
 h1 { font-size:17px; margin:0 0 16px; }
 input { width:100%; box-sizing:border-box; background:#0a0c10; color:#f2f4f7;
         border:0; border-radius:6px; padding:11px 13px;
         font:inherit; margin-bottom:14px; }
 input:focus { outline:1px solid rgba(255,255,255,.20); }
 button { width:100%; padding:8px 14px; font:inherit; cursor:pointer;
          background:#1d2025; border:0; border-radius:6px; color:#9aa1ad;
          transition:background .12s, color .12s; }
 button:hover:not(:disabled) { background:rgba(255,255,255,.09); color:#f2f4f7; }
 button:disabled { opacity:.5; cursor:default; }
 .msg { margin-top:14px; font-size:13px; }
 .msg.bad { color:#d86a6a; }
 .msg.ok { color:#63c77a; }
</style></head><body>
<div class="card">
  <h1 id="h">New password</h1>
  <form id="f">
    <input type="password" id="password" placeholder="New password"
           autocomplete="new-password" autofocus>
    <button type="submit" id="go">Save</button>
  </form>
  <div class="msg" id="msg"></div>
</div>
<script>
// Labels in the nine languages of the app (`mail_texts/<language>.json`, section
// `reset_page`); the language comes from the email link (`?lang=`), otherwise from the
// browser, otherwise English. The server fills it in when it builds the page: its own
// data, not third-party data.
const TEXTS = {{TEXTS}};
const MIN_PASSWORD = {{MIN}};
const asked = new URLSearchParams(location.search).get("lang");
const lang = [asked, ...(navigator.languages || [navigator.language || ""])]
  .map((x) => String(x || "").toLowerCase().split(/[-_]/)[0]).find((x) => TEXTS[x]) || "en";
const t = TEXTS[lang];
document.documentElement.lang = lang;
document.title = t.title;
document.getElementById("h").textContent = t.heading;
document.getElementById("password").placeholder = t.placeholder;
document.getElementById("go").textContent = t.button;
const say = (key) => String(t[key] || t.failed).replace("{min}", MIN_PASSWORD);
const token = new URLSearchParams(location.hash.slice(1)).get("token")
  || new URLSearchParams(location.search).get("token") || "";
if (!token) {
  document.getElementById("f").hidden = true;
  document.getElementById("msg").className = "msg bad";
  document.getElementById("msg").textContent = t.bad_link;
}
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const go = document.getElementById("go");
  const msg = document.getElementById("msg");
  const password = document.getElementById("password").value;
  if (password.length < MIN_PASSWORD) {
    msg.className = "msg bad";
    msg.textContent = say("short");
    return;
  }
  go.disabled = true;
  msg.className = "msg";
  msg.textContent = "";
  try {
    const r = await fetch("/auth/reset/confirm", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, password }),
    }).then((x) => x.json());
    if (!r.ok) {
      msg.className = "msg bad";
      msg.textContent = say(r.code === "link" ? "bad_link" : r.code);
      go.disabled = false;
      return;
    }
    msg.className = "msg ok";
    msg.textContent = t.saved;
    document.getElementById("f").hidden = true;
    history.replaceState(null, "", location.pathname);
  } catch (e) {
    msg.className = "msg bad";
    msg.textContent = t.offline;
    go.disabled = false;
  }
});
</script></body></html>"""


# The labels go into the page as JSON; `<` is escaped so that nothing in them can close
# `<script>`.
RESET_PAGE = (RESET_PAGE
              .replace("{{TEXTS}}", json.dumps({lang: mailtexts.reset_page(lang) for lang in mailtexts.LANGS},
                                               ensure_ascii=False).replace("<", chr(92) + "u003c"))
              .replace("{{MIN}}", str(accounts.MIN_PASSWORD)))


def _script_hash(page: str) -> str:
    """Hash of the page's only inline script, for `Content-Security-Policy`."""
    inline = re.search(r"<script>(.*?)</script>", page, re.DOTALL).group(1)
    return "sha256-" + base64.b64encode(hashlib.sha256(inline.encode("utf-8")).digest()).decode()


# The recovery page parses what arrives in the address, so it gets the strictest policy:
# only its own script runs (by hash), and it talks only to us. Even if an injection were
# ever found in it, the browser would not run a foreign script.
RESET_HEADERS = {
    "Content-Security-Policy": (
        f"default-src 'none'; script-src '{_script_hash(RESET_PAGE)}'; "
        "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


# Order number in the address: for values above `2**63`, sqlite answered `OverflowError`
# and a 500 on an address open to everyone. There are never more than a few million orders,
# so we take 2**31 as the limit.
OrderId = Annotated[int, PathParam(ge=1, le=2**31 - 1)]


def _why(errors: list[dict]) -> str:
    """What is wrong with the request, in one phrase for a human.

    FastAPI answers an invalid body with a list of dicts, and the app does not show such an
    answer to the person (it says nothing useful). They would only see "something went wrong".
    """
    for err in errors:
        loc = [str(x) for x in err.get("loc", ())]
        kind = str(err.get("type", ""))
        field = loc[-1] if loc else ""
        if "too_long" in kind:
            if field in ("system", "prompt"):
                return "too much text in the request — remove some of the materials"
            if "pinned_b64" in loc or field == "image_b64":
                return "too many or too heavy images — remove some of the pages"
            if field == "text":
                return "the text for voice replies is too long"
        if field == "max_tokens":
            return "the requested answer is too long"
    return "the request wasn't accepted — check what you entered"


def _reset_code(error: Exception) -> str:
    """What is wrong with the password change, in one word for the page. The page shows the
    message in the person's language.

    The page cannot translate the text of the error; it needs a code: `short`, `weak`,
    `link`, `busy`, otherwise empty.
    """
    if isinstance(error, accounts.Busy):
        return "busy"
    text = str(error)
    for start, code in (("password is shorter", "short"), ("the password is too simple", "weak"), ("link is invalid", "link")):
        if text.startswith(start):
            return code
    return ""


MONO_ASKS_PER_MINUTE = 60


def _payment_name(pay: billing.Provider) -> str | None:
    """What to tell the app about the payment provider. One thing matters to it: whether it works at
    all (the "top up" button is shown only if the answer is non-empty). Which provider
    it is is our own internal matter, like the models behind the relay; only the stub mode is
    named, and it exists only for the developer."""
    if pay.name == "off":
        return None
    return "stub" if pay.name == "stub" else "on"


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    # App language: emails are written in it. An old app does not send it; then the previous
    # language (or the default) is used.
    lang: str = Field(default="", max_length=16)


class Registration(Credentials):
    # The version of the terms the person saw under the button. Required: over the network, an
    # account without accepted terms is not created. The server does not compare it with its
    # own "current" version. It records what the app showed, and that record shows exactly which
    # text the person agreed to.
    terms: str = Field(min_length=1, max_length=32)


class TermsAccept(BaseModel):
    terms: str = Field(min_length=1, max_length=32)


class DeleteRequest(BaseModel):
    password: str = Field(max_length=200)


class RefreshRequest(BaseModel):
    refresh: str = Field(max_length=200)


class PasswordChange(BaseModel):
    old: str = Field(max_length=200)
    new: str = Field(max_length=200)


class ResetRequest(BaseModel):
    email: str = Field(max_length=254)
    lang: str = Field(default="", max_length=16)


class ResetConfirm(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(max_length=200)


class CheckoutRequest(BaseModel):
    dollars: int = Field(ge=1, le=100_000)
    # App language: the payment provider's payment page is shown in it, if it supports it.
    lang: str = Field(default="", max_length=8)


# An honest client will not send more than two minutes of audio in one report: reports go
# every ten seconds (`account/meter.py`), and what piled up during a connection loss it cuts
# into chunks of this size. The cap stops one bad number from charging the person for
# everything at once.
MAX_STT_REPORT_S = 120


class SttReport(BaseModel):
    seconds: float = Field(gt=0, le=MAX_STT_REPORT_S)
    # Accuracy tier at which the audio was processed: "more accurate" costs more. No field (an
    # old app) means the normal tier.
    tier: str = Field(default="normal", pattern="^(normal|high)$")
    # Report number: the client sends the same one with every retry (the response was lost, but
    # the charge went through), and the server does not charge a second time. An old app does not
    # send a number; then each report is separate, as before.
    report_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")


def build(db: sqlite3.Connection, secret: str,
          pay: billing.Provider | None = None,
          mail: mailer.Mailer | None = None, base_url: str = "",
          docs: bool | None = None) -> FastAPI:
    pay = pay or billing.Off()
    mail = mail or mailer.Off()
    # The link in the email must be complete: the email is read not on this server but anywhere,
    # and a relative path there just means a broken link. It is the same address as for the
    # payment provider (`ARGUS_SERVER_URL`): there is one server, and only it knows its own
    # address.
    base_url = base_url.rstrip("/")
    # In production, the API description (`/docs`, `/redoc`, `/openapi.json`) is closed. It
    # names every endpoint and field, the providers behind the relay, and the payment provider:
    # a ready-made map for anyone looking for where our money is. For development, enable it
    # with `ARGUS_SERVER_DOCS=1`.
    if docs is None:
        docs = os.environ.get("ARGUS_SERVER_DOCS", "").strip() == "1"
    # Low-balance emails: once every `sweep_s` seconds (`ARGUS_BALANCE_SWEEP_S`, 0 means off).
    # Without real mail (`Off`) they are not sent at all: an email that went nowhere must not
    # count as "sent".
    sweep_s = float(os.environ.get("ARGUS_BALANCE_SWEEP_S", "300") or 0)

    def send_now(address: str, message: mailtemplate.Message) -> None:
        mail.send(address, message.subject, message.text, html=message.html, images=message.images)

    async def balance_notices() -> None:
        while True:
            await asyncio.sleep(sweep_s)
            try:
                # The database is used only from here (the server thread); only the sending of one email
                # goes to a neighboring thread.
                for item in mailevents.balances_due(db):
                    try:
                        await asyncio.to_thread(send_now, item.address, item.message)
                    except Exception as e:
                        log.warning("balance email was not sent (account %s): %s", item.account_id, e)
                        continue
                    mailevents.mark_notified(db, item.account_id, item.level)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("balance sweep: unexpected failure")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if sweep_s > 0 and mail.name != "off":
            task = asyncio.create_task(balance_notices(), name="balance-notices")
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    web = FastAPI(title="Argus accounts",
                  docs_url="/docs" if docs else None,
                  redoc_url="/redoc" if docs else None,
                  openapi_url="/openapi.json" if docs else None,
                  lifespan=lifespan)
    # Request body limit, before everything else (see `limits.py`).
    web.add_middleware(limits.BodyLimit, secret=secret)
    # The proxy secret (a string) takes precedence over a plain "always trust": see
    # `limits.client_ip`.
    trust_proxy: bool | str = (os.environ.get("ARGUS_PROXY_SECRET", "").strip()
                               or os.environ.get("ARGUS_TRUST_PROXY", "").strip() == "1")
    # How the server determines the person's address, written to the log so nobody has to
    # guess. Without a secret, all requests through nginx look like one address, and the
    # attempt counters become shared by everyone (this both protects and hurts everyone at
    # once).
    if isinstance(trust_proxy, str):
        if len(trust_proxy) < 16:
            log.error("ARGUS_PROXY_SECRET is shorter than 16 characters — too easy to guess, set a longer one")
        log.info("the client address is taken from X-Forwarded-For only when the proxy secret is correct")
    elif trust_proxy:
        log.warning("ARGUS_TRUST_PROXY=1: the X-Forwarded-For header is always trusted — the server port must be closed")
    else:
        log.warning("ARGUS_PROXY_SECRET is not set: behind nginx all requests look like one address")
    # Whatever the previous process left on hold (it died in the middle of a paid call) is
    # returned.
    entitlements.recover_holds(db)

    @web.exception_handler(RequestValidationError)
    async def unreadable(request: Request, exc: RequestValidationError) -> JSONResponse:
        # The status is the same as before (422), but `detail` is a string: a list of dicts tells the
        # person nothing.
        return JSONResponse({"detail": _why(exc.errors())}, status_code=422)

    @web.exception_handler(Exception)
    async def broken(request: Request, exc: Exception) -> JSONResponse:
        # Without this, a bare "Internal Server Error" goes out. The details are in the log (the
        # server writes the exception anyway); the person gets one phrase.
        return JSONResponse({"detail": "the server couldn't handle the request — try later"},
                            status_code=500)

    # Attempt counters (see `throttle.py`). Keys: the email (normalized), the address, the
    # account. Each has two tiers: the short one does not stop a person from making a couple of
    # mistakes, and the long one prevents slow guessing. It is the same for existing and
    # nonexistent emails; otherwise the lockout itself would become a way to learn whether an
    # address is registered.
    #
    # Sign-in is counted per "email + address" pair: five misses from one address lock that
    # address out for that email, while the owner on another address is not affected by other
    # people's misses. The email alone gets only a higher cap, against guessing from many
    # addresses at once. A lone attacker on one address cannot reach it, so cannot lock the
    # owner out of their own account.
    login_by_pair = throttle.Window(((5, 15 * 60), (20, 24 * 3600)))
    login_by_mail = throttle.Window(((30, 15 * 60), (100, 24 * 3600)))
    login_by_ip = throttle.Window(((20, 15 * 60), (100, 24 * 3600)))
    signup_by_ip = throttle.Window(((20, 3600), (100, 24 * 3600)))
    # A sign-up gift is given at most three times per address per day (for IPv6, per /64
    # network). Past that, the account is still created, but without a gift. The overall daily
    # cap for the whole server is `accounts.GIFT_DAILY_CAP`.
    gift_by_ip = throttle.Window(((3, 24 * 3600),))
    # All sign-in attempts from an address, including successful ones: each costs a scrypt (a
    # pool of two threads, a queue of sixteen), and a flood of sign-ins with the right password
    # would occupy it for everyone. Sixty per minute leaves room for an office behind one
    # address.
    signin_rate_by_ip = throttle.Window(((60, 60),))
    # Emails to one address (except password recovery, which has its own limits): welcome,
    # password change, deletion, top-up. Without this, someone else's mailbox could be flooded
    # with emails from our domain, and the mail service's daily quota could be used up so that
    # even password recovery did not get through.
    mail_by_address = throttle.Window(((4, 3600), (12, 24 * 3600)))
    # Top-up orders from one account: this limits checking of stolen cards through our payment
    # provider, and table bloat.
    orders_by_account = throttle.Window(((6, 3600), (20, 24 * 3600)))
    # Password changes on one account (with the correct old password): each costs a scrypt and
    # an email.
    password_changes = throttle.Window(((10, 3600),))
    password_tries = throttle.Window(((5, 15 * 60), (20, 24 * 3600)))
    reset_by_ip = throttle.Window(((10, 3600), (40, 24 * 3600)))
    confirm_by_ip = throttle.Window(((30, 15 * 60),))

    def guard(window: throttle.Window, key: str | None, what: str) -> None:
        """429 if there are already too many for this key. Writes nothing."""
        if key is None:
            return
        wait = window.wait(key)
        if wait > 0:
            raise HTTPException(429, f"{what} — wait {throttle.wait_text(wait)}",
                                headers={"Retry-After": str(math.ceil(wait))})

    def busy(e: accounts.Busy) -> HTTPException:
        return HTTPException(503, str(e), headers={"Retry-After": "30"})

    async def caller(authorization: str = Header(default="")) -> int:
        """Who is asking. Without a valid pass: 401, no exceptions."""
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "sign-in required")
        try:
            body = tokens.read_access(secret, authorization[7:])
        except tokens.BadToken as e:
            raise HTTPException(401, str(e)) from None
        account_id = int(body["a"])
        # A short-lived pass lives for fifteen minutes and does not know by itself that the account
        # was deleted at the person's request or closed by the owner. Without this check, such an
        # account would keep calling the paid relays for another quarter of an hour on a live pass.
        # (The number of a deleted account is never reused, because of `AUTOINCREMENT` in
        # `db.SCHEMA`, so an old pass cannot come alive for a new person.)
        row = db.execute("SELECT blocked_at FROM accounts WHERE id = ?",
                         (account_id,)).fetchone()
        if row is None:
            raise HTTPException(401, "no account")
        if row["blocked_at"] is not None:
            raise HTTPException(401, "account is closed")
        return account_id

    async def send_mail(address: str, message: mailtemplate.Message, what: str) -> None:
        """Send an email, outside the event loop and after the response.

        SMTP is several network exchanges, each with a timeout. On the loop, one hung mailbox
        would hold the whole server. After the response, because the response time must not reveal
        whether an account exists (the email is sent only to an existing one). A mail failure must
        not change the response: the request, registration, or payment is already recorded, and an
        "error" would be a lie to the person. Loud in the log, quiet to the person.
        """
        if what != "password recovery":
            to = address.strip().lower()
            if mail_by_address.wait(to) > 0:
                log.warning("email not sent (%s): too many emails have already been sent to this address", what)
                return
            mail_by_address.add(to)
        try:
            await asyncio.to_thread(mail.send, address, message.subject, message.text,
                                    html=message.html, images=message.images)
        except mailer.MailError as e:
            # For recovery this is especially bad: the request is already recorded, a second attempt
            # within `RESET_COOLDOWN_S` will not create a new token, and the chance in this window is
            # lost silently.
            log.error("failed to send email (%s): %s", what, e)
        except Exception:
            log.exception("email (%s): unexpected failure", what)

    def queue(background: BackgroundTasks, sent: mailevents.Sent | None, what: str) -> None:
        """Put an email in the queue after the response; `None` means there is no one to send it."""
        if sent is not None:
            background.add_task(send_mail, sent[0], sent[1], what)

    @web.post("/auth/register")
    async def register(payload: Registration, request: Request, background: BackgroundTasks) -> dict:
        """Create an account. The gift to the balance is issued at once."""
        ip = limits.client_ip(request, trust_proxy)
        guard(signup_by_ip, ip, "too many sign-ups from this address")
        if ip:
            signup_by_ip.add(ip)          # every attempt costs a scrypt, whether it succeeded or not
        # The gift is given only while fewer than three accounts have been created from this address
        # in a day (the account is created in any case).
        gift = not ip or gift_by_ip.wait(ip) == 0
        try:
            out = await accounts.register_async(db, secret, payload.email, payload.password,
                                                terms=payload.terms, gift=gift)
            if ip:
                gift_by_ip.add(ip)
        except emails.BadEmail as e:
            raise HTTPException(400, str(e)) from None
        except accounts.Busy as e:
            raise busy(e) from None
        except accounts.Denied as e:
            raise HTTPException(409, str(e)) from None
        mailevents.remember_lang(db, payload.email, payload.lang)
        queue(background, mailevents.welcome(db, payload.email), "welcome")
        return _entered(out)

    @web.post("/auth/sign-in")
    async def sign_in(payload: Credentials, request: Request) -> dict:
        ip = limits.client_ip(request, trust_proxy)
        try:
            key = emails.check(payload.email)
        except emails.BadEmail as e:
            raise HTTPException(400, str(e)) from None
        # The "locked" response is the same for any email, registered or not.
        pair = f"{key}|{ip or ''}"
        guard(signin_rate_by_ip, ip, "too many sign-in attempts from this address")
        if ip:
            signin_rate_by_ip.add(ip)
        guard(login_by_pair, pair, "too many sign-in attempts")
        guard(login_by_mail, key, "too many sign-in attempts")
        guard(login_by_ip, ip, "too many sign-in attempts from this address")
        try:
            out = await accounts.sign_in_async(db, secret, payload.email, payload.password)
        except accounts.Busy as e:
            raise busy(e) from None
        except accounts.Denied as e:
            login_by_pair.add(pair)
            login_by_mail.add(key)
            if ip:
                login_by_ip.add(ip)
            if (login_by_pair.wait(pair) > 0 or login_by_mail.wait(key) > 0
                    or (ip and login_by_ip.wait(ip) > 0)):
                # A series of failures, attempts are locked. The log gets the address and a short trace of
                # the email (the email itself is not written to the log), so that the trace shows someone is
                # guessing.
                log.warning("login: series of failures, attempts blocked (address %s, email #%s)", ip,
                            hashlib.sha256(key.encode()).hexdigest()[:8])
            raise HTTPException(403, str(e)) from None
        login_by_pair.clear(pair)
        login_by_mail.clear(key)
        mailevents.remember_lang(db, payload.email, payload.lang)
        return _entered(out)

    def _entered(out: accounts.Login) -> dict:
        return {
            "refresh": out.refresh,
            "access": out.access,
            "right": entitlements.right(db, out.account_id).as_json(),
        }

    @web.post("/auth/refresh")
    async def refresh(payload: RefreshRequest) -> dict:
        try:
            return {"access": accounts.refresh(db, secret, payload.refresh)}
        except accounts.Denied as e:
            raise HTTPException(401, str(e)) from None

    @web.post("/auth/sign-out", status_code=204)
    async def sign_out(payload: RefreshRequest) -> None:
        accounts.sign_out(db, payload.refresh)

    @web.post("/auth/password", status_code=204)
    async def password(payload: PasswordChange, background: BackgroundTasks,
                       account_id: int = Depends(caller)) -> None:
        # The old password can be guessed here by someone holding a stolen pass. The count is shared
        # with account deletion: wrong passwords are counted per account, not per request.
        tries = f"a{account_id}"
        guard(password_tries, tries, "too many wrong passwords")
        guard(password_changes, tries, "the password was changed too often")
        try:
            await accounts.change_password_async(db, account_id, payload.old, payload.new)
        except accounts.Busy as e:
            raise busy(e) from None
        except accounts.WrongPassword as e:
            password_tries.add(tries)
            raise HTTPException(403, str(e)) from None
        except accounts.Denied as e:
            raise HTTPException(403, str(e)) from None
        password_tries.clear(tries)
        password_changes.add(tries)
        queue(background, mailevents.password_changed(db, account_id), "password changed")

    # ----------------------------------------------------------------
    # A person's data: terms, export, deletion
    # ----------------------------------------------------------------

    @web.post("/account/terms", status_code=204)
    async def accept_terms(payload: TermsAccept,
                           account_id: int = Depends(caller)) -> None:
        """Accept the terms: for old accounts and for a new version of the text."""
        accounts.accept_terms(db, account_id, payload.terms)

    @web.get("/account/export")
    async def export(account_id: int = Depends(caller)) -> dict:
        """Everything we know about the account in one response: the right to a copy of the data."""
        try:
            return accounts.export_data(db, account_id)
        except accounts.Denied as e:
            raise HTTPException(401, str(e)) from None

    @web.post("/account/delete", status_code=204)
    async def delete(payload: DeleteRequest, background: BackgroundTasks,
                     account_id: int = Depends(caller)) -> None:
        """Delete the account. The password is asked again: the token could have remained on someone
        else's machine.
        """
        tries = f"a{account_id}"
        guard(password_tries, tries, "too many wrong passwords")
        address = mailevents.address_of(db, account_id)         # after deletion, the address is no longer in the database
        try:
            await accounts.delete_account_async(db, account_id, payload.password)
        except accounts.Busy as e:
            raise busy(e) from None
        except accounts.WrongPassword as e:
            password_tries.add(tries)
            raise HTTPException(403, str(e)) from None
        except accounts.Denied as e:
            raise HTTPException(403, str(e)) from None
        if address:
            queue(background, mailevents.deleted(address), "account deleted")

    # ----------------------------------------------------------------
    # Password recovery
    # ----------------------------------------------------------------

    @web.post("/auth/reset/request", status_code=204)
    async def reset_request(payload: ResetRequest, request: Request,
                            background: BackgroundTasks) -> None:
        """Recovery request. The response is always the same.

        204 with no body and no error, whether or not an account with this email exists, whether
        or not to expect an email, and whether or not one was sent recently. The difference is
        visible only in whether the email arrives, not in the server's response. A response that
        told these cases apart would leak the list of addresses registered with us.
        """
        ip = limits.client_ip(request, trust_proxy)
        # Requests from one address are limited. Otherwise the form is a "flood other people's
        # mailboxes and burn through our daily mail limit" button, as long as accounts are free.
        guard(reset_by_ip, ip, "too many recovery requests from this address")
        if ip:
            reset_by_ip.add(ip)
        started = accounts.begin_reset(db, payload.email)
        if started is None:
            return
        raw, address = started
        # The language of the request takes precedence over the previous one: the person may have
        # changed the app language. The link carries the accepted language code, not the string that
        # was sent.
        lang = mailevents.remember_lang(db, payload.email, payload.lang) or mailevents.stored_lang_by_email(db, payload.email)
        # The token goes in the part after "#": it is not sent to the server and does not end up in
        # its log.
        link = f"{base_url}/auth/reset" + (f"?lang={lang}" if lang else "") + f"#token={raw}"
        queue(background, mailevents.reset_password(address, link, accounts.RESET_TTL_S // 60, lang),
              "password recovery")

    @web.get("/auth/reset", response_class=HTMLResponse)
    async def reset_page() -> HTMLResponse:
        """The page from the email: one field, the new password.

        The change itself happens in the page's own JS, with a POST to `/auth/reset/confirm`, the
        same way the stub payment provider calls its own `/billing/stub/{id}`. We do not check the
        token here: the check lives where the change is applied. Otherwise it is easy to be off by
        one step and give two paths of the same check different answers.

        The server never sees the token and does not put it into the page. The token comes from
        the address, so from a link that anyone could have crafted, and any insertion of a foreign
        string into a script is an injection (`json.dumps` does not touch `</script>`). The page's
        script (`RESET_PAGE`) reads it, and the `RESET_HEADERS` policy lets nothing else execute.
        """
        return HTMLResponse(RESET_PAGE, headers=RESET_HEADERS)

    @web.post("/auth/reset/confirm")
    async def reset_confirm(payload: ResetConfirm, request: Request, background: BackgroundTasks) -> dict:
        # The page's script reads the responses of this endpoint, so a refusal is also
        # `{"ok": false, "error": …}`.
        ip = limits.client_ip(request, trust_proxy)
        try:
            guard(confirm_by_ip, ip, "too many attempts")
        except HTTPException as e:
            return {"ok": False, "error": e.detail, "code": "limit"}
        if ip:
            confirm_by_ip.add(ip)
        try:
            key = await accounts.finish_reset_async(db, payload.token, payload.password)
        except (accounts.Busy, accounts.Denied) as e:
            return {"ok": False, "error": str(e), "code": _reset_code(e)}
        # The email was confirmed by the link from the email, so whoever is behind it is the owner
        # of the mailbox, and the sign-in lock that someone guessing may have set must not get in
        # their way.
        login_by_mail.clear(key)
        login_by_pair.clear(f"{key}|{ip or ''}")
        account_id = mailevents.account_of_reset(db, payload.token)
        if account_id is not None:
            queue(background, mailevents.password_changed(db, account_id), "password changed")
        return {"ok": True}

    # ----------------------------------------------------------------
    # Payment
    # ----------------------------------------------------------------

    @web.get("/pricing")
    async def price_list() -> dict:
        """Prices and top-up. `payment` says whether the payment provider works at all.

        Without it, the app cannot tell "payment is not connected" from "the button is broken",
        and the person keeps pressing it until they write to support.
        """
        return {
            **pricing.price_sheet(),
            "payment": _payment_name(pay),
        }

    @web.post("/billing/checkout")
    async def checkout(payload: CheckoutRequest,
                       account_id: int = Depends(caller)) -> dict:
        """Create a top-up order and say where to go to pay."""
        key = f"a{account_id}"
        guard(orders_by_account, key, "too many top-up orders")
        orders_by_account.add(key)
        row = db.execute(
            "SELECT email FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        try:
            order = billing.start(db, pay, account_id, payload.dollars,
                                  row["email"] if row else "", lang=payload.lang)
        except billing.BillingError as e:
            raise HTTPException(400, str(e)) from None
        return order.as_json()

    @web.get("/billing/stub/{order_id}", response_class=HTMLResponse)
    async def stub_page(order_id: OrderId) -> str:
        """The stub's "payment" page. It takes no money and says so.

        It exists only with `ARGUS_BILLING=stub`. In any other mode it answers 404, so that it
        cannot be called by a direct address.
        """
        if pay.name != "stub":
            raise HTTPException(404, "there is no such page")
        return STUB_PAGE.replace("{order}", str(int(order_id)))

    @web.post("/billing/stub/{order_id}")
    async def stub_pay(order_id: OrderId, background: BackgroundTasks) -> dict:
        if pay.name != "stub":
            raise HTTPException(404, "there is no such page")
        try:
            fresh = billing.paid(db, order_id, f"stub-{order_id}")
        except billing.BillingError as e:
            raise HTTPException(400, str(e)) from None
        if fresh:
            queue(background, mailevents.top_up(db, int(order_id)), "top-up")
        return {"paid": True, "first_time": fresh}

    # -- LiqPay: exists only with `ARGUS_BILLING=liqpay` --------------

    @web.post("/billing/liqpay/callback")
    async def liqpay_notice(request: Request, background: BackgroundTasks) -> dict:
        """LiqPay's payment notification, the only place where a payment is credited.

        The address is open to everyone, so nothing here is trusted except the signature.
        `billing.liqpay_callback` checks it, together with the amount and the order.
        """
        if not isinstance(pay, billing.LiqPay):
            raise HTTPException(404, "there is no such page")
        body = await request.body()
        if len(body) > billing.MAX_FORM_BYTES:
            raise HTTPException(413, "the request is too large")
        fields = billing.form_fields(body)
        try:
            result = billing.liqpay_callback(
                db, pay, fields.get("data", ""), fields.get("signature", ""))
        except billing.BillingError as e:
            raise HTTPException(400, str(e)) from None
        if result == "paid":
            # The signature was already checked in `liqpay_callback`. We take the order number from the
            # same data, and the email reads the amount and balance from the database, not from the
            # notification.
            order_id = pay.order_of(pay.read(fields.get("data", ""), fields.get("signature", "")))
            if order_id is not None:
                queue(background, mailevents.top_up(db, int(order_id)), "top-up")
        return {"ok": True, "result": result}

    @web.get("/billing/liqpay/{order_id}", response_class=HTMLResponse)
    async def liqpay_page(order_id: OrderId, lang: str = "") -> str:
        """Redirect to LiqPay: the signed form is built here and submits itself.

        The signature and the key stay with us; the address the person sees does not carry them.
        A paid order is not paid a second time: instead of the form, the return page is shown.
        """
        if not isinstance(pay, billing.LiqPay):
            raise HTTPException(404, "there is no such page")
        row = db.execute(
            "SELECT amount, state, provider FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row is None or row["provider"] != pay.name:
            raise HTTPException(404, "there is no such order")
        if row["state"] != "new":
            return DONE_PAGE
        page = LIQPAY_PAGE
        form = pay.form(order_id, int(row["amount"]), lang.strip().lower())
        for key, value in form.items():
            page = page.replace("{" + key + "}", html.escape(value, quote=True))
        return page

    # -- plata by mono: exists only with `ARGUS_BILLING=mono` ---------

    # The notification and return addresses are open to everyone, and for every real order we
    # ask the payment provider ourselves. So that a flood of forged requests does not turn into
    # a flood of questions to mono (and its "too often" limit does not block real payments), we
    # ask no more than `MONO_ASKS_PER_MINUTE` questions per minute. A refusal here is harmless:
    # mono repeats the notification, and the person's return picks up the order on the next
    # visit.
    mono_asked: list[float] = []

    def mono_may_ask() -> bool:
        now = time.monotonic()
        while mono_asked and now - mono_asked[0] > 60:
            mono_asked.pop(0)
        if len(mono_asked) >= MONO_ASKS_PER_MINUTE:
            return False
        mono_asked.append(now)
        return True

    def mono_order(order_id: int) -> sqlite3.Row | None:
        return db.execute(
            "SELECT amount, state, provider, external_id FROM orders WHERE id = ? AND provider = ?",
            (int(order_id), "mono")).fetchone()

    @web.get("/billing/mono/{order_id}")
    async def mono_page(order_id: OrderId):
        """Redirect to mono: the invoice is created here (not when the order is placed), and the
        person is sent to the payment page.

        Each visit revokes the previous unpaid invoice and creates a new one, so an order has one
        live invoice. A paid order is not paid a second time: instead of the redirect, the return
        page is shown.
        """
        if not isinstance(pay, billing.Mono):
            raise HTTPException(404, "there is no such page")
        row = mono_order(order_id)
        if row is None:
            raise HTTPException(404, "there is no such order")
        if row["state"] != "new":
            return HTMLResponse(DONE_PAGE)
        previous = str(row["external_id"] or "")
        try:
            if previous.startswith("mono-"):
                await asyncio.to_thread(pay.remove_invoice, previous[len("mono-"):])
            invoice, url = await asyncio.to_thread(pay.create_invoice, int(order_id), int(row["amount"]))
        except billing.BillingError:
            return HTMLResponse(MONO_ERROR_PAGE, status_code=502)
        db.execute("UPDATE orders SET external_id = ? WHERE id = ? AND state = 'new'",
                   (f"mono-{invoice}", int(order_id)))
        db.commit()
        return RedirectResponse(url, status_code=302)

    @web.post("/billing/mono/callback")
    async def mono_notice(request: Request, background: BackgroundTasks) -> dict:
        """mono notification: it hints which invoice to ask about. It does not credit the money; the
        payment provider's own answer does.

        We do not parse the signature (ECDSA). Whatever arrives at this address, the credit
        follows what mono answers to our question about the invoice (`billing.mono_settle`), so
        forging a notification changes nothing.
        """
        if not isinstance(pay, billing.Mono):
            raise HTTPException(404, "there is no such page")
        raw = await request.body()
        if len(raw) > billing.MAX_FORM_BYTES:
            raise HTTPException(413, "the request is too large")
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            raise HTTPException(400, "bad request body")        # read not by a person but by the mono server
        invoice = data.get("invoiceId")
        order_id = pay.order_of(data) if billing.mono_invoice_ok(invoice) else None
        row = mono_order(order_id) if order_id is not None else None
        if row is None or row["state"] not in ("new", "paid"):
            log.info("mono: notification is not about a live order (invoice %r, reference %r)", invoice, data.get("reference"))
            return {"ok": True, "result": "ignored"}
        if not mono_may_ask():
            raise HTTPException(429, "too many requests")       # mono will repeat the notification
        try:
            status = await asyncio.to_thread(pay.status, str(invoice))
        except billing.BillingError as e:
            raise HTTPException(502, str(e)) from None          # mono will repeat the notification
        result = billing.mono_settle(db, pay, status)
        if result == "paid":
            settled = pay.order_of(status)
            if settled is not None:
                queue(background, mailevents.top_up(db, int(settled)), "top-up")
        return {"ok": True, "result": result}

    @web.get("/billing/mono/back/{order_id}", response_class=HTMLResponse)
    async def mono_back(order_id: OrderId, background: BackgroundTasks) -> str:
        """Where mono returns the person. If the notification got lost, the order is completed here: we
        ask the provider about the order's last invoice and credit it if it is paid. The page asserts
        nothing either way."""
        if not isinstance(pay, billing.Mono):
            raise HTTPException(404, "there is no such page")
        row = mono_order(order_id)
        if row is None:
            raise HTTPException(404, "there is no such order")
        external = str(row["external_id"] or "")
        if row["state"] == "new" and external.startswith("mono-") and mono_may_ask():
            try:
                status = await asyncio.to_thread(pay.status, external[len("mono-"):])
            except billing.BillingError:
                return DONE_PAGE
            if billing.mono_settle(db, pay, status) == "paid":
                queue(background, mailevents.top_up(db, int(order_id)), "top-up")
        return DONE_PAGE

    @web.api_route("/billing/done", methods=["GET", "POST"], response_class=HTMLResponse)
    async def billing_done() -> str:
        """Where the payment provider returns the person. It may return with a GET as well as a POST,
        and a 405 instead of a page would look like a breakage: we answer both."""
        return DONE_PAGE

    @web.get("/billing/orders")
    async def orders(account_id: int = Depends(caller)) -> dict:
        rows = db.execute(
            "SELECT id, amount, currency, state, created_at, paid_at FROM orders "
            "WHERE account_id = ? ORDER BY created_at DESC LIMIT 20",
            (account_id,),
        ).fetchall()
        return {"orders": [dict(r) for r in rows]}

    @web.get("/billing/history")
    async def history(account_id: int = Depends(caller)) -> dict:
        """Where an account's money came from: the gift, top-ups, manual grants.

        From the `credits` log, not from orders: a gift and a grant have no order, and a person who
        opens the history expects to see everything that landed on the account. It also adds up
        to the balance (balance = log minus spending).
        """
        rows = db.execute(
            "SELECT kind, micro, at FROM credits WHERE account_id = ? "
            "ORDER BY at DESC, id DESC LIMIT 30",
            (account_id,),
        ).fetchall()
        return {"credits": [
            {"kind": r["kind"], "micro": int(r["micro"]),
             "amount": pricing.money(int(r["micro"])), "at": r["at"]}
            for r in rows
        ]}

    @web.get("/billing/usage")
    async def usage(days: int = 30, tz: int = 0,
                    account_id: int = Depends(caller)) -> dict:
        """Spending by day, for the chart on the payment page."""
        days = max(1, min(90, days))
        return {
            "days": days,
            "daily": entitlements.usage_daily(db, account_id, days, tz),
        }

    @web.get("/me")
    async def me(account_id: int = Depends(caller)) -> dict:
        row = db.execute(
            "SELECT email, created_at, terms_version FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(401, "no account")
        return {
            "email": row["email"],
            "since": row["created_at"],
            # Which version of the terms the person accepted; empty means they did not accept (the
            # account is older than the terms). The app uses this to decide whether to offer acceptance.
            "terms": row["terms_version"],
            "right": entitlements.right(db, account_id).as_json(),
            "spent": entitlements.spent_summary(db, account_id),
        }

    @web.post("/meter/stt")
    async def meter_stt(payload: SttReport,
                        account_id: int = Depends(caller)) -> dict:
        """The client reports how many seconds of audio went to recognition.

        Needed for a build that recognizes speech with its own key, bypassing us
        (`realtime_stt.relay` is off): the server knows about the usage only from the client's word.
        So this is accounting, not a lock: an honest client pays for what it used and stops when
        `allowed` becomes false. For audio through the relay (`relay_stt.py`), the server counts
        itself, and nothing is sent here.

        A report with a `report_id` is idempotent: a repeat with the same number charges nothing
        and answers the same as the first time (same shape, the allowance as of now).
        """
        entitlements.charge(db, account_id, "stt_seconds", payload.seconds,
                            pricing.stt_cost(payload.seconds, payload.tier),
                            report_id=payload.report_id)
        return {"right": entitlements.right(db, account_id).as_json()}

    relay.install(web, db, caller)

    return web


def from_env() -> FastAPI:
    """A server configured by environment variables. The entry point for deployment."""
    secret = os.environ.get("ARGUS_SERVER_SECRET", "")
    if len(secret) < 32:
        # A weak secret here means forged passes and free access for everyone. The server must fail
        # at startup instead of finding this out from the monthly bill.
        raise SystemExit("ARGUS_SERVER_SECRET of at least 32 characters is required")

    db = connect(Path(os.environ.get("ARGUS_SERVER_DB", "argus-accounts.sqlite")))
    base_url = os.environ.get("ARGUS_SERVER_URL", "")
    return build(db, secret, pay=billing.from_env(base_url), mail=mailer.from_env(),
                base_url=base_url)
