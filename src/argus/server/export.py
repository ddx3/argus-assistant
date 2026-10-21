"""Export everything the server knows about an account, at a request that reached the
operator by email.

The privacy policy (section 6) promises a copy of the data within 30 days. The export code
(`accounts.export_data`) is available only to the person themselves, with their pass; this
command gives the operator the same answer by email address. A password and tokens are
never in it. Run it on the server:

    docker exec argus-server python3 -m argus.server.export email@example.com > copy.json

Erasing an account at the same kind of request is `argus.server.erase`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from argus.server import accounts, emails
from argus.server.db import connect


def export(db, email: str) -> dict:
    """The same as the person would get themselves from `/account/export`. `SystemExit` if there is
    no such account.
    """
    try:
        key = emails.normalize(email)
    except emails.BadEmail as e:
        raise SystemExit(str(e)) from None
    row = db.execute("SELECT id FROM accounts WHERE email_key = ?", (key,)).fetchone()
    if row is None:
        raise SystemExit(f"account {email} does not exist")
    return accounts.export_data(db, int(row["id"]))


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("email required: python3 -m argus.server.export email@example.com")
    db = connect(Path(os.environ.get("ARGUS_SERVER_DB", "argus-accounts.sqlite")))
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(export(db, sys.argv[1]), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
