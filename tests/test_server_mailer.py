"""Mail: where an email goes, how the connection is protected, and what happens with a bad
setting.

`mailer.py` used to have no tests at all, and three holes in it went unnoticed: STARTTLS
without certificate verification (anyone on the path to the mail service could read the
password and the recovery links), recipients that the library pulled out of the "To"
header, and an empty string `ARGUS_SMTP_PORT=` that brought the whole server down at
startup.
"""

from __future__ import annotations

import smtplib
import ssl
from typing import ClassVar

import pytest

from argus.server import mailer


class FakeSMTP:
    """Stands in for a real connection: remembers everything that was done with it."""

    made: ClassVar[list[FakeSMTP]] = []
    fail_with: ClassVar[Exception | None] = None

    def __init__(self, host, port, timeout=None, context=None):
        if FakeSMTP.fail_with is not None:
            raise FakeSMTP.fail_with
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.starttls_context: ssl.SSLContext | None = None
        self.started_tls = False
        self.login_as: tuple[str, str] | None = None
        self.sent: list[tuple] = []
        FakeSMTP.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True
        self.starttls_context = context

    def login(self, user, password):
        self.login_as = (user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent.append((msg, from_addr, to_addrs))


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.made = []
    FakeSMTP.fail_with = None
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def smtp(**over) -> mailer.SMTP:
    cfg = {"host": "mail.example.com", "port": 587, "user": "robot@example.com",
           "password": "mail-secret", "sender": "Argus <noreply@argus.example>"}
    cfg.update(over)
    return mailer.SMTP(**cfg)


# --------------------------------------------------------------------------
# Recipient
# --------------------------------------------------------------------------


def test_the_envelope_has_exactly_one_recipient_and_the_bare_sender():
    smtp().send("user@example.com", "Subject", "Body")
    (_, from_addr, to_addrs), = FakeSMTP.made[0].sent
    assert to_addrs == ["user@example.com"]
    assert from_addr == "noreply@argus.example"       # without "Argus <…>": the bare address goes into the envelope


@pytest.mark.parametrize("hostile", [
    "a<victim@gmail.com", "x,victim@gmail.com", "x:victim@gmail.com", "a;victim@gmail.com",
    "a@example.com\nBcc: victim@gmail.com", "a b@example.com", "a@example.com, victim@gmail.com",
    "no-at-sign", "",
])
def test_an_address_that_hides_another_one_gets_no_letter_at_all(hostile):
    """`a<victim@gmail.com` passed validation, while `send_message` took the recipients from the
    header.
    """
    with pytest.raises(mailer.MailError):
        smtp().send(hostile, "Subject", "Body")
    assert FakeSMTP.made == [], "the mail server was not even connected to"


def test_the_message_header_carries_the_same_address_as_the_envelope():
    smtp().send("user@bücher.example", "Subject", "Body")
    msg, _, to_addrs = FakeSMTP.made[0].sent[0]
    assert to_addrs == ["user@xn--bcher-kva.example"]
    assert str(msg["To"]) == to_addrs[0]


# --------------------------------------------------------------------------
# Connection protection
# --------------------------------------------------------------------------


def test_starttls_verifies_the_certificate_and_the_host_name():
    smtp().send("user@example.com", "Subject", "Body")
    conn = FakeSMTP.made[0]
    assert conn.started_tls
    ctx = conn.starttls_context
    assert isinstance(ctx, ssl.SSLContext), "without a context the library takes \"verify nothing\""
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert conn.login_as == ("robot@example.com", "mail-secret")


def test_the_login_goes_only_after_the_connection_is_encrypted():
    order = []

    class Ordered(FakeSMTP):
        def starttls(self, context=None):
            order.append("tls")
            super().starttls(context)

        def login(self, user, password):
            order.append("login")
            super().login(user, password)

    mailer.smtplib.SMTP = Ordered
    smtp().send("user@example.com", "Subject", "Body")
    assert order == ["tls", "login"]


def test_port_465_is_encrypted_from_the_first_byte_with_the_same_checks():
    smtp(port=465).send("user@example.com", "Subject", "Body")
    conn = FakeSMTP.made[0]
    assert conn.port == 465
    assert not conn.started_tls, "STARTTLS is not needed on top of implicit TLS"
    assert isinstance(conn.context, ssl.SSLContext)
    assert conn.context.verify_mode == ssl.CERT_REQUIRED and conn.context.check_hostname


def test_encryption_can_be_switched_off_only_by_asking():
    smtp(use_tls=False, port=25).send("user@example.com", "Subject", "Body")
    assert not FakeSMTP.made[0].started_tls


def test_every_exchange_has_a_timeout():
    smtp().send("user@example.com", "Subject", "Body")
    assert FakeSMTP.made[0].timeout == 10.0


@pytest.mark.parametrize("failure", [
    smtplib.SMTPAuthenticationError(535, b"bad"), smtplib.SMTPServerDisconnected("dropped"),
    ConnectionRefusedError("no connection"), ssl.SSLCertVerificationError("wrong certificate"),
    TimeoutError("too long"),
])
def test_a_failed_send_is_a_mail_error_not_a_crash(failure):
    FakeSMTP.fail_with = failure
    with pytest.raises(mailer.MailError):
        smtp().send("user@example.com", "Subject", "Body")


# --------------------------------------------------------------------------
# Setup from the environment
# --------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    for name in ("ARGUS_MAIL", "ARGUS_SMTP_HOST", "ARGUS_SMTP_PORT", "ARGUS_SMTP_USER",
                 "ARGUS_SMTP_PASSWORD", "ARGUS_SMTP_FROM", "ARGUS_SMTP_TLS"):
        monkeypatch.delenv(name, raising=False)

    def put(**values):
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    return put


def test_without_a_host_there_is_no_mail(env):
    assert mailer.from_env().name == "off"


def test_the_console_stub_is_asked_for_by_name(env):
    env(ARGUS_MAIL="console")
    assert mailer.from_env().name == "console"


def test_a_blank_port_line_in_the_env_file_does_not_stop_the_server(env):
    """`ARGUS_SMTP_PORT=` gives an empty string, and `int('')` crashed the whole server at
    startup.
    """
    env(ARGUS_SMTP_HOST="mail.example.com", ARGUS_SMTP_PORT="", ARGUS_SMTP_USER="robot@example.com")
    sender = mailer.from_env()
    assert sender.name == "smtp" and sender.port == 587


def test_a_port_that_is_not_a_number_switches_the_mail_off_and_says_so(env, caplog):
    env(ARGUS_SMTP_HOST="mail.example.com", ARGUS_SMTP_PORT="five-hundred")
    assert mailer.from_env().name == "off"
    assert "ARGUS_SMTP_PORT" in caplog.text


def test_the_whole_setting_is_read(env):
    env(ARGUS_SMTP_HOST="mail.example.com", ARGUS_SMTP_PORT="465",
        ARGUS_SMTP_USER="robot@example.com", ARGUS_SMTP_PASSWORD="mail-secret",
        ARGUS_SMTP_FROM="Argus <noreply@argus.example>", ARGUS_SMTP_TLS="1")
    sender = mailer.from_env()
    assert (sender.host, sender.port, sender.user) == ("mail.example.com", 465, "robot@example.com")
    assert sender.sender == "Argus <noreply@argus.example>"
    assert sender.use_tls is True


def test_the_sender_defaults_to_the_mailbox_we_log_in_with(env):
    env(ARGUS_SMTP_HOST="mail.example.com", ARGUS_SMTP_USER="robot@example.com")
    assert mailer.from_env().sender == "robot@example.com"


def test_tls_is_on_unless_it_is_switched_off_explicitly(env):
    env(ARGUS_SMTP_HOST="mail.example.com")
    assert mailer.from_env().use_tls is True
    env(ARGUS_SMTP_TLS="0")
    assert mailer.from_env().use_tls is False


# --------------------------------------------------------------------------
# A designed email: markup, the image inside, service headers
# --------------------------------------------------------------------------


def test_an_html_letter_is_text_plus_html_with_the_logo_inside():
    from argus.server import mailevents

    _, message = mailevents.reset_password("user@example.com", "https://api.argus.example/auth/reset#token=t", 30, "en")
    smtp().send("user@example.com", message.subject, message.text, html=message.html, images=message.images)
    msg = FakeSMTP.made[0].sent[0][0]
    assert msg.get_content_type() == "multipart/alternative"
    assert msg.get_body(preferencelist=("plain",)).get_content().strip() == message.text.strip()
    assert "cid:argus-logo" in msg.get_body(preferencelist=("html",)).get_content()
    images = [part for part in msg.walk() if part.get_content_maintype() == "image"]
    assert len(images) == 1
    assert images[0]["Content-ID"] == "<argus-logo>"
    assert images[0].get_content_disposition() == "inline"
    assert images[0].get_content() == message.images["argus-logo"]


def test_a_plain_letter_stays_a_single_text_part():
    smtp().send("user@example.com", "Subject", "Body")
    msg = FakeSMTP.made[0].sent[0][0]
    assert msg.get_content_type() == "text/plain"
    assert not msg.is_multipart()


def test_the_letter_has_a_date_a_message_id_on_our_domain_and_says_it_is_automatic():
    smtp().send("user@example.com", "Subject", "Body")
    msg = FakeSMTP.made[0].sent[0][0]
    assert msg["Date"]
    assert msg["Message-ID"].endswith("@argus.example>")
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["Reply-To"] is None


def test_replies_go_to_the_configured_address():
    smtp(reply_to="support@argus.example").send("user@example.com", "Subject", "Body")
    assert FakeSMTP.made[0].sent[0][0]["Reply-To"] == "support@argus.example"


def test_reply_to_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("ARGUS_SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("ARGUS_SMTP_REPLY_TO", " support@argus.example ")
    assert mailer.from_env().reply_to == "support@argus.example"
