"""Passwords that are too simple: the ones tried first.

A minimum of eight characters cuts off the short ones, but not "12345678" or "qwertyui":
those are exactly what opens first in a dictionary attack. The list is short and
hand-made: a full dictionary would pull in a dependency for a couple of hundred of the
most frequent strings, and the rest is covered by the attempt counter (`throttle.py`) and
scrypt.
"""

from __future__ import annotations

from itertools import pairwise

COMMON = frozenset("""
12345678 123456789 1234567890 12345678910 11111111 00000000 987654321 1234512345 123123123
12341234 123412345 12345679 1q2w3e4r 1q2w3e4r5t 1qaz2wsx 1qazxsw2 q1w2e3r4 q1w2e3r4t5
qwertyui qwertyuiop qwerty12 qwerty123 qwerty1234 qwerty123456 qwertyuiop123 qwe12345
qwe123qwe qazwsxedc qazwsx123 zxcvbnm1 zxcvbnm123 asdfghjk asdfghjkl asdf1234 asdfasdf
zaq12wsx password password1 password12 password123 password1234 password01 passw0rd
p@ssw0rd p@ssword pass1234 pass12345 pa$$w0rd iloveyou iloveyou1 iloveyou123 letmein1
letmein123 welcome1 welcome123 admin123 admin1234 administrator abc12345 abcd1234
abcdefgh abcdefg1 abc123456 monkey123 dragon12 superman football football1 baseball
basketball sunshine princess trustno1 starwars internet computer whatever mustang1
master123 shadow12 michael1 jordan23 jennifer charlie1 changeme changeme1 default1
secret123 login123 test1234 testtest guest123 user1234 hello123 hellohello 123qwe123
1234qwer 1234qwerty 123456qwerty 123456a 123456789a a123456789 a1234567 a12345678
qq123456 qqqqqqqq 11223344 12121212 12344321 1234abcd abcd12345 12qwaszx
йцукенгш йцукенгшщзх йцуйцуйцу пароль123 пароль12 пароль1234 пароль01 парольпароль
привет123 приветпривет
""".split())  # noqa: SIM905 - the list is readable by eye while it is in columns


def _ladder(p: str) -> bool:
    """"12345678", "87654321", "abcdefgh": each character is one step further (or closer)."""
    if len(p) < 6:
        return False
    steps = {ord(b) - ord(a) for a, b in pairwise(p)}
    return steps == {1} or steps == {-1}


def is_weak(password: str, email: str = "") -> bool:
    """A password of the kind that is guessed first. `email` is there so that an email is not
    accepted in place of a password.
    """
    p = password.lower()
    if p in COMMON or len(set(p)) <= 2 or _ladder(p):
        return True
    mail = email.strip().lower()
    return bool(mail) and p in (mail, mail.split("@")[0])
