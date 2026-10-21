"""Manual balance top-up is not a product but an operator command.

It is for whoever runs the server: to put money on their own account or a friend's without
sending it through the payment provider. It is deliberately not an HTTP endpoint: topping
up with a button reachable over the network means creating a way to give yourself money
without control, and the only entry check here must be who has `docker exec` on this
machine.

Run it on the server, the amount in dollars (100 by default):

    docker exec argus-server python3 -m argus.server.grant email@example.com 25
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from argus.server import emails, entitlements, pricing
from argus.server.db import connect

DEFAULT_DOLLARS = 100


def grant(db, email: str, dollars: float = DEFAULT_DOLLARS) -> int:
    """Put `dollars` on the account. Returns the new balance, in microdollars."""
    try:
        key = emails.normalize(email)
    except emails.BadEmail as e:
        raise SystemExit(str(e)) from None
    row = db.execute("SELECT id FROM accounts WHERE email_key = ?", (key,)).fetchone()
    if row is None:
        raise SystemExit(f"account {email} does not exist — register in the app first")

    micro = pricing.micro_of(dollars)
    new = entitlements.credit(db, row["id"], micro, "grant", note="manual grant")
    print(f"{email}: +{pricing.money(micro)}, balance {pricing.money(new)}")
    return new


def main() -> int:
    if len(sys.argv) not in (2, 3):
        raise SystemExit("an email and, optionally, an amount are required: "
                         "python3 -m argus.server.grant email@example.com 25")
    dollars = float(sys.argv[2]) if len(sys.argv) == 3 else DEFAULT_DOLLARS
    if dollars <= 0:
        raise SystemExit("the amount must be greater than zero")
    db = connect(Path(os.environ.get("ARGUS_SERVER_DB", "argus-accounts.sqlite")))
    grant(db, sys.argv[1], dollars)
    return 0


if __name__ == "__main__":
    sys.exit(main())
