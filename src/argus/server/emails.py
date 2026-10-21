"""Email: format check and reduction to a single key.

We check exactly two things: that the address looks like an address, and that the same
mailbox is not created twice because of different letter case. There are no lists of
disposable domains and no DNS queries. Registration is ordinary, because people pay for
access and do not go through a security check.
"""

from __future__ import annotations

import re

# By the RFC an address can be almost anything, but we do not need to accept anything: we
# need to reject the obviously unfit before the person presses "next" and decides that the
# account was created.
#
# The left part is only the characters the RFC allows without quotes (`atext`), and dots
# between them. Previously anything except "@" and a space was accepted, and
# `a<victim@mail.example` passed: from the "To" header the mail library extracted
# `victim@mail.example` and sent it a recovery email in our name. "<", ">", ",", ":", ";",
# quotes, and brackets are not allowed here. The right part is labels separated by dots;
# the last one starts with a letter (as in `xn--p1ai` too).
_ATEXT = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]"
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
SHAPE = re.compile(
    rf"^(?=[^@]{{1,64}}@)({_ATEXT}+(?:\.{_ATEXT}+)*)@((?:{_LABEL}\.)+[a-z][a-z0-9-]*[a-z0-9])$"
)
MAX_DOMAIN = 253


class BadEmail(ValueError):
    """The address is unsuitable. The message text goes to the person as is."""


_BAD = "the address should look like name@domain"


def normalize(email: str) -> str:
    """The key by which addresses are treated as the same.

    Case and surrounding spaces, and the domain also goes to ASCII (the IDN example domain
    `пример.рф` and `xn--e1afmkfd.xn--p1ai` are one mailbox). We do not touch dots and
    plus tags: on most mail services `j.doe@` and `jdoe@` are different mailboxes of different
    people, and by merging them we would hand one person someone else's account.
    """
    email = email.strip()
    if email.count("@") != 1:
        raise BadEmail(_BAD)
    # The left part is ASCII only, checked before lowercasing. Otherwise the Kelvin sign "K"
    # (U+212A) turned into an ordinary "k", the key took someone else's address (`kevin@...`),
    # and the email went to "Kevin@..." with a non-mail letter: the owner of the real address
    # could neither create an account nor recover it.
    if not email.partition("@")[0].isascii():
        raise BadEmail(_BAD)
    email = email.lower()
    local, domain = email.split("@")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        raise BadEmail(_BAD) from None
    return f"{local}@{domain}"


def check(email: str) -> str:
    """Check the address and return its key. Raises `BadEmail` with an explanation."""
    key = normalize(email)
    match = SHAPE.match(key)
    if match is None or len(match.group(2)) > MAX_DOMAIN:
        raise BadEmail(_BAD)
    return key


def ascii_address(email: str) -> str:
    """Address for an email: the left part as typed, the domain in ASCII (SMTP accepts it that
    way).

    Raises `BadEmail` if the address would not pass `check`: an email goes only to an address
    that passed the same check as at registration.
    """
    key = check(email)
    local = email.strip().rsplit("@", 1)[0]
    return f"{local}@{key.rsplit('@', 1)[1]}"
