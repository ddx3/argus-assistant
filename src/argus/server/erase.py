"""Erase an account at a request that reached the operator by email, without a password.

The privacy policy (section 6) refers people to an email to the operator and promises a
reply within 30 days. The deletion code (`accounts.delete_account`) is available only to
the person themselves, with their pass and password; for the operator to write `DELETE` by
hand would risk forgetting to reattach the money to `-id` (see `accounts.erase_account`).
This command does the same as the button, but by email address and without a password:
the email from the account's address is itself the confirmation.

By default it only shows what would be erased; it deletes with `--yes`. Run it on the
server:

    docker exec argus-server python3 -m argus.server.erase email@example.com
    docker exec argus-server python3 -m argus.server.erase email@example.com --yes

Exporting a person's data at the same kind of email is `argus.server.export`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from argus.server import accounts, emails, pricing
from argus.server.db import connect


def summary(db, email: str) -> dict:
    """What the account has and what will become of it. `SystemExit` if there is no such account."""
    try:
        key = emails.normalize(email)
    except emails.BadEmail as e:
        raise SystemExit(str(e)) from None
    row = db.execute(
        "SELECT id, email, balance_micro FROM accounts WHERE email_key = ?", (key,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"account {email} does not exist")
    count = {
        table: db.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id = ?",
                          (row["id"],)).fetchone()[0]
        for table in ("orders", "credits", "usage", "sessions")
    }
    return {"id": int(row["id"]), "email": row["email"],
            "balance_micro": int(row["balance_micro"]), **count}


def erase(db, email: str, confirmed: bool = False) -> dict:
    """Erase the account with this email (if `confirmed`). Returns what `summary` shows."""
    info = summary(db, email)
    if confirmed:
        accounts.erase_account(db, info["id"])
    return info


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--yes"]
    if len(args) != 1:
        raise SystemExit("email required: python3 -m argus.server.erase email@example.com [--yes]")
    confirmed = "--yes" in sys.argv[1:]
    db = connect(Path(os.environ.get("ARGUS_SERVER_DB", "argus-accounts.sqlite")))
    info = erase(db, args[0], confirmed)
    left = pricing.money(info["balance_micro"])
    what = (f"account #{info['id']} ({info['email']}): balance {left}, "
            f"orders {info['orders']}, top-ups {info['credits']}, "
            f"usage records {info['usage']}, sign-ins {info['sessions']}")
    if confirmed:
        print(f"erased — {what}. The money and usage are left without an owner (under -{info['id']}).")
    else:
        print(f"will be erased — {what}. The account balance will be lost. To confirm, add --yes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
