"""The app side: sign-in and balance.

It decides nothing; it only asks and shows the answer. This is not carelessness but
design: the program sits on someone else's computer, and any check inside it is cut out in
ten minutes. The right to recognition is computed by the server, and here lives what
should live on the client: where the token is kept, when to refresh it, and what to show
the person.

One secret is stored, the refresh token, in `argus.account.json` next to the settings. The
short-lived access pass is not stored at all: it lives for fifteen minutes and is obtained
again from the refresh token.

The network may be absent. Then `state()` returns the last thing it knew, marked
`offline`: showing the person "no connection to the server" is more honest than "access
closed", and certainly more honest than an empty screen.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from argus.account.legal import TERMS_VERSION
from argus.core import friendly

log = logging.getLogger(__name__)

STORE_NAME = "argus.account.json"


def _owner_only(path: Path) -> None:
    """The file with the refresh token is readable only by its owner.

    The token lives three months and opens an account that holds money. It sits next to the
    program, and the folder it was unpacked into is often readable by all users of the
    computer (drive D:, "Downloads" on a shared PC, cloud sync). If locking it down fails,
    sign-in works anyway: a refusal here is no reason to lose the sign-in.
    """
    try:
        if sys.platform == "win32":
            user = os.environ.get("USERNAME", "").strip()
            if not user:
                return
            # We drop permission inheritance from the folder and leave only the owner.
            # `CREATE_NO_WINDOW`: a built program has no console, and without the flag a window would
            # flash.
            subprocess.run(
                [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"),
                 str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
                capture_output=True, timeout=10, creationflags=0x08000000, check=False)
        else:
            os.chmod(path, 0o600)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("permissions on the login file are not restricted: %s", e)

# The pass lives 15 minutes; we refresh it ahead of time so that a request does not run into
# the boundary.
ACCESS_REFRESH_BEFORE_S = 120
TIMEOUT_S = 10.0

# Calls to `/relay/*` answer only when the paid upstream has finished: for an ordinary
# answer that takes seconds, for "think longer" and for long text, tens of seconds. The
# server finishes such a call and charges for it even if the client has already left, so we
# have to wait longer than the server is willing to work: its ceiling for the whole request
# is 150 s (`UPSTREAM_DEADLINE_S` in `server/relay.py`). Someone who hung up earlier would
# pay for an answer they never saw. The connection itself, as everywhere, is dropped after
# `TIMEOUT_S`: only the answer itself is waited for long.
RELAY_READ_TIMEOUT_S = 160.0


def _timeout_for(path: str) -> httpx.Timeout | float:
    if path.startswith("/relay/"):
        return httpx.Timeout(TIMEOUT_S, read=RELAY_READ_TIMEOUT_S)
    return TIMEOUT_S


class AccountError(Exception):
    """The server refused. The text is to be shown to the person as is."""


def json_of(r, *keys: str) -> dict:
    """The server response body as a dict, or an `AccountError` in human words.

    A response with code 200 may not be from us: a hotel Wi-Fi login page, a proxy stub, a
    redirect. `r.json()` on such a page raises a raw `JSONDecodeError`, and it must not reach
    callers who expect only `AccountError` from this module: the offline snapshot (`state`)
    and the model response (`RelayProvider.ask`) use it to decide whether this is "no
    connection" or not. `keys` are the fields without which the response is useless (an old or
    foreign server).
    """
    try:
        body = r.json()
    except ValueError:
        log.warning("accounts server responded with something unexpected: body is not JSON")
        raise AccountError(friendly.NETWORK) from None
    if not isinstance(body, dict):
        log.warning("accounts server responded with something unexpected, not an object: %s",
                    type(body).__name__)
        raise AccountError(friendly.NETWORK)
    missing = [k for k in keys if k not in body]
    if missing:
        log.warning("accounts server response is missing fields: %s", ", ".join(missing))
        raise AccountError(friendly.DOWN)
    return body


class Account:
    """The app's link to the account server."""

    def __init__(self, base_url: str, root: Path):
        self.base = base_url.rstrip("/")
        self.path = root / STORE_NAME
        self._lock = threading.Lock()
        self._access = ""
        self._access_until = 0.0
        self._last: dict | None = None
        # For `state_soon()`: the last server response, when it was received, and whether a refresh
        # is currently running in a thread.
        self._soon: dict | None = None
        self._soon_at = 0.0
        self._soon_busy = False
        self._data = self._read()

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _owner_only(self.path)
        except OSError as e:
            log.warning("could not save the login: %s", e)

    @property
    def signed_in(self) -> bool:
        return bool(self._data.get("refresh"))

    @property
    def email(self) -> str:
        return str(self._data.get("email", ""))

    # ------------------------------------------------------------------
    # Talking to the server
    # ------------------------------------------------------------------

    def _send(self, method: str, path: str, timeout, headers: dict, kw: dict) -> httpx.Response:
        try:
            return httpx.request(method, f"{self.base}{path}", timeout=timeout,
                                 headers=headers, **kw)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            # Details go to the log: the network library's text ("[Errno 11001] getaddrinfo failed")
            # tells the person nothing.
            log.warning("no connection to the accounts server: %s: %s", type(e).__name__, e)
            raise AccountError(friendly.explain(e, trust_text=False)) from None

    def _call(
        self, method: str, path: str, *, auth: bool = False, timeout=None, **kw,
    ) -> httpx.Response:
        """A request to the server. Any failure is an `AccountError`; no raw network errors come out
        of here.

        `timeout` defaults to `TIMEOUT_S`, and for `/relay/*` it is long
        (`RELAY_READ_TIMEOUT_S`): a paid answer arrives when the upstream has finished, and the
        client must not hang up before the server does.
        """
        headers = kw.pop("headers", {})
        if timeout is None:
            timeout = _timeout_for(path)
        for attempt in (1, 2):
            sent = dict(headers)
            if auth:
                # The token exchange is also a trip to the network, and its failure is also an
                # `AccountError`.
                sent["Authorization"] = f"Bearer {self._pass()}"
            r = self._send(method, path, timeout, sent, kw)
            if r.status_code == 401 and auth and attempt == 1:
                # A 401 can come from the short-lived pass alone: the server was deployed with a different
                # secret, the clock shifted, the pass was revoked. The refresh token may still be alive, so
                # we exchange it again and repeat the request once. Forgetting the sign-in is decided only
                # by `_pass`, when the refresh token itself is dead.
                self._drop_access()
                continue
            break
        if 300 <= r.status_code < 400 or (
            r.status_code < 300 and "text/html" in r.headers.get("content-type", "").lower()
        ):
            # Our server answers with neither a redirect nor a web page: that is how a foreign network
            # answers (hotel Wi-Fi login, proxy stub). Calls that do not parse the response body
            # (account deletion, password change) would take this for "it worked".
            log.warning("accounts server gave an unrecognized response: code %s, %s", r.status_code,
                        r.headers.get("content-type", ""))
            raise AccountError(friendly.NETWORK)
        if r.status_code >= 400:
            detail = ""
            try:
                body = r.json()
                detail = body.get("detail", "") if isinstance(body, dict) else ""
            except ValueError:
                detail = r.text[:200]
            # Request body validation answers with a list of errors (422), which tells a person nothing.
            # That is how, for example, a server answers that has not yet been updated to the new
            # checkout: we show a generic refusal, not a Python list of dicts. The same goes for the
            # English "Not Found" and for an error page from a proxy: we show only what is written for
            # people.
            if not isinstance(detail, str) or not friendly.plain(detail):
                detail = ""
            # A sign-in that is no longer valid (the token was revoked, the account closed, the password
            # changed) is forgotten by `_pass`: its refusal at the token exchange is exactly "the
            # sign-in is dead". Here the 401 comes after a fresh pass, so we leave the sign-in alone: it
            # is just a refusal.
            raise AccountError(detail or friendly.by_status(r.status_code))
        return r

    def _drop_access(self) -> None:
        """Forget the short pass in memory: the next `_pass()` will exchange the refresh token."""
        with self._lock:
            self._access = ""
            self._access_until = 0.0

    def access_token(self) -> str:
        """A short-lived pass for things that open outside `_call`, for example the recognition
        socket.

        It is refreshed ahead of time (`ACCESS_REFRESH_BEFORE_S`), so a freshly opened connection
        does not run into the expiry boundary. Raises `AccountError` if there is no sign-in or it
        has been lost.
        """
        return self._pass()

    def ws_url(self, path: str) -> str:
        """The address of the same server for WebSocket: `http` -> `ws`, `https` -> `wss`."""
        base = self.base
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):] + path
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):] + path
        return "ws://" + base + path

    def _pass(self) -> str:
        """The short pass: from memory, or exchanged for a refresh token."""
        with self._lock:
            if self._access and time.time() < self._access_until - ACCESS_REFRESH_BEFORE_S:
                return self._access
        refresh = self._data.get("refresh")
        if not refresh:
            raise AccountError("you need to sign in")
        # The very first request after the app starts (the pass lives only in memory), and every one
        # fifteen minutes later, comes here: without a connection this is an ordinary refusal, not a
        # raw exception from the network library (somebody higher up would catch it at once, or
        # would not, and then `/control` answered 500 instead of "no connection, let it through").
        try:
            r = httpx.post(f"{self.base}/auth/refresh", timeout=TIMEOUT_S,
                           json={"refresh": refresh})
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            log.warning("no connection to the accounts server: %s: %s", type(e).__name__, e)
            raise AccountError(friendly.explain(e, trust_text=False)) from None
        if r.status_code == 401:
            self.forget()
            raise AccountError("session expired — sign in again")
        if r.status_code >= 400:
            raise AccountError(friendly.by_status(r.status_code))
        access = json_of(r, "access")["access"]
        if not isinstance(access, str) or not access:
            raise AccountError(friendly.DOWN)
        with self._lock:
            self._access = access
            self._access_until = time.time() + 15 * 60
            return self._access

    # ------------------------------------------------------------------
    # Sign-in
    # ------------------------------------------------------------------

    def register(self, email: str, password: str, terms: str = TERMS_VERSION, lang: str = "") -> dict:
        """Create an account. The gift to the balance is issued at once.

        `terms` is the version of the terms that the person saw under the button. The page does
        not send it: the version lives in the app (`account/legal.py`), and the page showing one
        version while another is sent is ruled out.

        `lang` is the app's language: the server writes emails in it (welcome, payment, balance).
        """
        return self._entered("/auth/register", email, password, {"terms": terms}, lang)

    def sign_in(self, email: str, password: str, lang: str = "") -> dict:
        return self._entered("/auth/sign-in", email, password, None, lang)

    def _entered(self, path: str, email: str, password: str, extra: dict | None = None, lang: str = "") -> dict:
        r = self._call("POST", path,
                       json={"email": email, "password": password, **(extra or {}), **({"lang": lang} if lang else {})})
        body = json_of(r, "refresh", "access")
        # We do not store the password anywhere: only the refresh token lies on disk. A stored
        # password would have to be either encrypted with a key lying next to it or kept as is, and
        # both are worse than signing in once every three months.
        self._data = {"refresh": body["refresh"], "email": email.strip()}
        self._write()
        with self._lock:
            self._access = body["access"]
            self._access_until = time.time() + 15 * 60
            self._soon = None           # signed in under a different account, so the old response is no good
        self._last = body.get("right")
        return body

    def request_reset(self, email: str, lang: str = "") -> None:
        """A request for password recovery.

        No sign-in is needed, which is what is used when the password is forgotten. The server's
        answer is always the same, whether an account with this email exists or not: the
        difference is visible only from the email, not from the response.
        """
        self._call("POST", "/auth/reset/request", json={"email": email, **({"lang": lang} if lang else {})})

    def sign_out(self) -> None:
        refresh = self._data.get("refresh")
        if refresh:
            try:
                self._call("POST", "/auth/sign-out", json={"refresh": refresh})
            except AccountError:
                # The server is unavailable, but we sign out anyway. Leaving the token "until the
                # connection returns" would show the person that they are signed in after they pressed
                # "sign out".
                pass
        self.forget()

    def forget(self) -> None:
        self._data = {}
        with self._lock:
            self._access = ""
            self._access_until = 0.0
            self._soon = None
        self._last = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Entitlement
    # ------------------------------------------------------------------

    def state(self) -> dict:
        """What to show in settings. Without a network, the last known state."""
        if not self.signed_in:
            return {"signed_in": False, "email": "", "right": None, "offline": False}
        try:
            body = json_of(self._call("GET", "/me", auth=True), "right", "email")
        except AccountError as e:
            # A dead sign-in and a broken network look the same (both raise `AccountError`) but require
            # different things from the person: in the first case they must sign in again, in the
            # second they need do nothing. The two are told apart by `forget()`, which happened inside
            # on a 401.
            if not self.signed_in:
                return {"signed_in": False, "email": "", "right": None,
                        "offline": False, "error": str(e)}
            return {
                "signed_in": True,
                "email": self.email,
                "right": self._last,
                "offline": True,
                "error": str(e),
            }
        self._last = body["right"]
        return {
            "signed_in": True,
            "email": body["email"],
            "right": body["right"],
            "spent": body.get("spent"),
            # The version of the terms the person accepted; empty means they did not accept.
            "terms": body.get("terms"),
            "offline": False,
        }

    def state_soon(self, max_age: float = 10.0) -> dict:
        """The same as `state()`, but does not wait for the network: returns the last known state,
        and if it is older than `max_age` seconds, refreshes it in a background thread.

        For the dashboard state snapshot: it is assembled twice a second right on the event loop,
        and `state()` is a request to the account server (about 100 ms, up to `TIMEOUT_S` on a
        bad connection). Made inside the snapshot itself, it would hold the whole server for that
        time every half second: the windows would respond in jerks, and when the connection
        dropped they would freeze for ten seconds.

        Until there is a first answer, it returns "no connection" (`offline`): callers in that
        case forbid nothing and show no hint, since a false refusal is worse than a hint that
        arrives a hundredth of a second later.
        """
        if not self.signed_in:
            return self.state()                        # no network, so fast
        with self._lock:
            stale = self._soon is None or time.monotonic() - self._soon_at >= max_age
            start = stale and not self._soon_busy
            if start:
                self._soon_busy = True
            known = self._soon
        if start:
            threading.Thread(target=self._refresh_soon, name="account-state", daemon=True).start()
        if known is not None:
            return known
        return {"signed_in": True, "email": self.email, "right": self._last, "offline": True}

    def _refresh_soon(self) -> None:
        state: dict | None = None
        try:
            state = self.state()
        except Exception:  # noqa: BLE001 — a background refresh must not fail silently into nowhere
            log.exception("could not refresh the account state")
        finally:
            with self._lock:
                if state is not None and self.signed_in:
                    self._soon = state
                self._soon_at = time.monotonic()
                self._soon_busy = False

    @property
    def known_right(self) -> dict | None:
        """The last known entitlement, without going to the network.

        For those who need to decide quickly and on their own thread: whether there is enough
        money to send audio to the cloud. It is refreshed by any server response that carries the
        entitlement (`state`, `report_stt`).
        """
        return self._last

    def accept_terms(self, terms: str = TERMS_VERSION) -> None:
        """Accept the terms: for an account created before them, or for a new version."""
        self._call("POST", "/account/terms", auth=True, json={"terms": terms})
        with self._lock:
            self._soon = None           # the previous server answer still says "not accepted"

    def change_password(self, old: str, new: str) -> None:
        """Change the password. The server closes **all** sign-ins, including this one.

        Passwords are most often changed over a suspicion that the old one leaked, so the server
        (`accounts.change_password`) kills all the account's sessions at once, and our refresh
        token is dead afterwards. So that the person does not find themselves signed out of their
        own app right after "Password changed", we sign in again with the new
        password they just entered. If signing in fails (the connection dropped), we forget the
        token and say so plainly: the password is already the new one, and they will have to sign
        in with it.
        """
        email = self.email
        self._call("POST", "/auth/password", auth=True, json={"old": old, "new": new})
        try:
            self.sign_in(email, new)
        except AccountError:
            self.forget()
            raise AccountError("the password was changed, but signing in again failed — sign in with the new password") from None

    def export_data(self) -> dict:
        """Everything the server knows about the account, for "Download my data"."""
        return json_of(self._call("GET", "/account/export", auth=True))

    def delete_account(self, password: str) -> None:
        """Delete the account for good (the "Delete account" button). The server asks for the password again.

        After deletion there is no sign-in any more, and the local token is removed the same way
        as on sign-out: leaving it would show the person that they are signed in to an account
        that does not exist.
        """
        self._call("POST", "/account/delete", auth=True, json={"password": password})
        self.forget()

    def pricing(self) -> dict:
        """Prices and top-up methods. No sign-in needed: the price is shown to everyone."""
        return json_of(self._call("GET", "/pricing"))

    def history(self) -> dict:
        """Top-ups and gifts, for the "History" table."""
        return json_of(self._call("GET", "/billing/history", auth=True))

    def usage(self, tz_minutes: int = 0) -> dict:
        """Spending by day, for the chart. `tz_minutes` is the person's time zone."""
        return json_of(self._call("GET", "/billing/usage", auth=True,
                                  params={"tz": int(tz_minutes)}))

    def checkout(self, dollars: int, lang: str = "") -> dict:
        """Create a top-up order and get the address where payment is made.

        `lang` is the app's language: the payment provider will show its page in it, if it can.
        An empty one is not sent: an old server does not expect the extra field.
        """
        body: dict = {"dollars": int(dollars)}
        if lang:
            body["lang"] = lang
        return json_of(self._call("POST", "/billing/checkout", auth=True, json=body))

    def report_stt(self, seconds: float, tier: str = "normal",
                   report_id: str | None = None) -> dict:
        """Tell the server how many seconds of audio went to recognition.

        `tier` is the accuracy level the audio was processed at: "more accurate" costs more. The
        normal one is not sent at all: an old server does not expect the extra field, and has a
        single price.

        `report_id` is the report number: the same on every retry of one and the same report. A
        server that knows it will not charge for it a second time (the answer could have been
        lost after the charge); an old one will simply ignore the extra field.

        Returns the entitlement after the charge. It is also remembered as the last known one,
        including in the fast state snapshot (`state_soon`): the dashboard must learn that the
        money has run out now, not after the ten seconds for which it caches the server poll.
        """
        report: dict = {"seconds": round(seconds, 2)}
        if tier != "normal":
            report["tier"] = tier
        if report_id:
            report["report_id"] = report_id
        body = json_of(self._call("POST", "/meter/stt", auth=True, json=report), "right")
        right = body["right"]
        self._last = right
        with self._lock:
            if self._soon is not None:
                self._soon = {**self._soon, "right": right}
        return right
