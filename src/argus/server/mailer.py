"""Sending emails. There is one provider, plain SMTP, and it plugs itself in.

There is one email here: the password recovery link. While there is only one, a separate
external mailing service does not pay off. SMTP is enough, and a mailbox for it already
exists. The design of the emails is in `mailtemplate.py`: what arrives here is already
finished (text, markup, images).

## The stub

As with the payment provider (`billing.py`), the stub is not "not done yet" but a working
tool: the path up to password recovery is checked end to end, and the link simply goes to
the log instead of someone else's mailbox. It is enabled with `ARGUS_MAIL=console`. The
default is `Off`: nobody sends emails, and a recovery request answers just as if the email
had gone out, because the secret of whether an account exists must not depend on whether
mail is set up on the server.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Protocol

from argus.server import emails

log = logging.getLogger(__name__)

SMTPS_PORT = 465        # implicit TLS: the connection is encrypted from the first byte, no STARTTLS


class MailError(Exception):
    """The email did not go out. The text goes to the log; to the person: "try later"."""


class Mailer(Protocol):
    name: str

    def send(self, to: str, subject: str, text: str, html: str | None = None,
             images: dict[str, bytes] | None = None) -> None:
        """Send an email. Raises `MailError` if it fails.

        `html` is the designed version (built by `mailtemplate.render`), `images` are the pictures
        for it (`cid` -> PNG): they travel inside the email itself, not as links.
        """
        ...


class Off:
    """No mail. The only honest behavior is to say so out loud in the log rather than swallow it: an
    email that nobody noticed did not go out is a person who waits a week for a link that was not
    and will not be sent."""

    name = "off"

    def send(self, to: str, subject: str, text: str, html: str | None = None,
             images: dict[str, bytes] | None = None) -> None:
        log.warning("mail is not configured — the email was not sent: %s / %s", to, subject)


class Console:
    """A stub for development: the whole email goes to the log, including the link.

    The entire path is checked (request, email, redirect, password change) without creating a
    real mailbox and without spending its limits on every run.
    """

    name = "console"

    def send(self, to: str, subject: str, text: str, html: str | None = None,
             images: dict[str, bytes] | None = None) -> None:
        log.warning("EMAIL (stub, not sent anywhere):\nTo: %s\nSubject: %s\n\n%s",
                    to, subject, text)


@dataclass
class SMTP:
    """Ordinary SMTP. From the library: there is no need to build our own client here."""

    name = "smtp"
    host: str
    port: int
    user: str
    password: str
    sender: str
    use_tls: bool = True
    # Where replies go: the sender address (`noreply@`) does not accept mail.
    reply_to: str = ""

    def send(self, to: str, subject: str, text: str, html: str | None = None,
             images: dict[str, bytes] | None = None) -> None:
        # An email goes only to an address that passed the same check as at registration.
        try:
            recipient = emails.ascii_address(to)
        except emails.BadEmail:
            raise MailError("the recipient address is not usable for an email") from None
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = recipient
        # Date and Message-ID: mailboxes are more willing to put an email without them into spam.
        # `Auto-Submitted` is so that the auto-responders of other people's mailboxes do not reply
        # to it.
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(domain=parseaddr(self.sender)[1].rpartition("@")[2] or None)
        msg["Auto-Submitted"] = "auto-generated"
        if self.reply_to:
            msg["Reply-To"] = self.reply_to
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
            # Images travel inside the email and belong to its markup (`cid:`); they are not attached
            # alongside.
            body = msg.get_body(preferencelist=("html",))
            for cid, data in (images or {}).items():
                body.add_related(data, "image", "png", cid=f"<{cid}>", disposition="inline")
        # Checking the certificate and the server name is mandatory: without `context=` the library
        # takes the "verify nothing" context, and anyone on the path to the mail service could read
        # both the login and password and every recovery link.
        context = ssl.create_default_context()
        try:
            if self.port == SMTPS_PORT:
                server = smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=10.0)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=10.0)
            with server:
                if self.use_tls and self.port != SMTPS_PORT:
                    server.starttls(context=context)
                if self.user:
                    server.login(self.user, self.password)
                # The envelope is set explicitly: otherwise the library extracts the recipients from the
                # headers itself, and everyone who ended up in "To" would receive the email.
                server.send_message(msg, from_addr=parseaddr(self.sender)[1] or self.sender,
                                    to_addrs=[recipient])
        except (OSError, smtplib.SMTPException) as e:
            raise MailError(f"couldn't send the email: {e}") from None


def from_env() -> Mailer:
    """Mail from environment variables. The default is "no mail"."""
    kind = os.environ.get("ARGUS_MAIL", "").strip().lower()
    if kind == "console":
        log.warning("MAIL IN STUB MODE: emails go to the log, not to the recipient")
        return Console()

    host = os.environ.get("ARGUS_SMTP_HOST", "").strip()
    if not host:
        return Off()

    user = os.environ.get("ARGUS_SMTP_USER", "").strip()
    # An empty string in `.env` (`ARGUS_SMTP_PORT=`) means "not set", not zero: `int('')`
    # crashed the whole server at startup, and with it accounts, payment, and relays, because of
    # a mail setting.
    raw_port = os.environ.get("ARGUS_SMTP_PORT", "").strip() or "587"
    try:
        port = int(raw_port)
    except ValueError:
        log.error("ARGUS_SMTP_PORT=%r — not a number, mail disabled", raw_port)
        return Off()
    return SMTP(
        host=host,
        port=port,
        user=user,
        password=os.environ.get("ARGUS_SMTP_PASSWORD", ""),
        # Without its own sender address, use the mailbox we send from: mail services usually
        # require `From` to match the identity that SMTP introduced.
        sender=os.environ.get("ARGUS_SMTP_FROM", user),
        use_tls=os.environ.get("ARGUS_SMTP_TLS", "1").strip().lower() not in ("0", "false", "no"),
        reply_to=os.environ.get("ARGUS_SMTP_REPLY_TO", "").strip(),
    )
