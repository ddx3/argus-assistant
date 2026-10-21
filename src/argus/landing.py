"""Where to open the page at launch.

A separate module, not part of `cli.py`: the built app calls it right after the server
answers, and pulling in `typer` and `rich` (the whole `argus.cli`) for the sake of one
function would mean paying for them on every start.
"""

from __future__ import annotations

from argus.core.config import Config


def landing_path(cfg: Config) -> str:
    """Where to open the page at launch: normally the dashboard (`""`), but until an account is
    configured and signed in, straight to the account section.

    Without signing in, the "recognize" button on the dashboard will refuse ("you need to sign in
    — it's in settings"), so without this the first screen after download is a
    working control panel where nothing turns on, and why is said only inside the settings,
    which one still has to think of opening. The check is local, with no network:
    `Account.signed_in` reads only a file on disk.

    The address is `/#account`, not `/admin#account`: the server redirects `/admin` to
    `/#admin` and loses the tab marker, while `chrome.js` on the main page itself opens the
    settings window on the account tab.
    """
    if not cfg.account.server:
        return ""
    from argus.account.client import Account

    account = Account(cfg.account.server, cfg.root)
    return "" if account.signed_in else "/#account"
