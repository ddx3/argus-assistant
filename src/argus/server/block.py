"""Close an account (or reopen it): an operator command for the owner.

It is needed when an account burns someone else's money or breaks the terms. Until now the
only way to close one was raw SQL. A closed account cannot sign in or refresh its pass,
and a live short-lived pass stops working at once (`app.caller` looks at `blocked_at`).
Run it on the server:

    docker exec argus-server python3 -m argus.server.block email@example.com
    docker exec argus-server python3 -m argus.server.block email@example.com --undo
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from argus.server import emails
from argus.server.db import connect


def block(db, email: str, undo: bool = False, now: float | None = None) -> int:
    """Close an account (with `undo`, open it). Returns its number. `SystemExit` if there is none."""
    try:
        key = emails.normalize(email)
    except emails.BadEmail as e:
        raise SystemExit(str(e)) from None
    row = db.execute("SELECT id FROM accounts WHERE email_key = ?", (key,)).fetchone()
    if row is None:
        raise SystemExit(f"account {email} does not exist")
    account_id = int(row["id"])
    if undo:
        db.execute("UPDATE accounts SET blocked_at = NULL WHERE id = ?", (account_id,))
    else:
        db.execute("UPDATE accounts SET blocked_at = ? WHERE id = ?",
                   (time.time() if now is None else now, account_id))
        # Sign-ins are closed together with the account: a refresh token without an account is
        # garbage in the database.
        db.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,))
    db.commit()
    return account_id


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--undo"]
    if len(args) != 1:
        raise SystemExit("email required: python3 -m argus.server.block email@example.com [--undo]")
    undo = "--undo" in sys.argv[1:]
    db = connect(Path(os.environ.get("ARGUS_SERVER_DB", "argus-accounts.sqlite")))
    account_id = block(db, args[0], undo)
    print(f"account #{account_id} ({args[0]}): " + ("open" if undo else "closed"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
