"""Documents: which version is in force and what the person has already confirmed.

The texts live in `legal/` (the original in English and translations into all the app's
languages) and are built by `legal/build.py` into two copies: the pages `terms.html` and
`privacy.html` next to the download page, and the same pages inside the app
(`ui/static/legal-*.html`). The app shows its own copy in a window over the page and does
not send the person to the browser: pressed "Terms" in the app, they read in the
app; pressed it on the landing page, they read in the browser. The app only needs to know
the version that the person saw.

The version is the date of the text edit. It should be raised when the meaning changes,
not a comma: it determines whether the app will ask for consent again. The server keeps
the previous version next to the account (`accounts.terms_version`), and it shows which
text the person agreed to.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

# The terms and the privacy policy are raised together: this is one package, and "accepted
# the old terms but the new policy" is a state that cannot be explained.
TERMS_VERSION = "2026-10-14"    # 2026-10-14: Mac, account deletion by email, how we announce changes

# Version of the notice before the first time recording is turned on. It is separate:
# changing the notice text does not mean changing the terms, and vice versa.
NOTICE_VERSION = "2026-10-06"

STORE_NAME = "argus.legal.json"


# The documents the app shows. They live as pages `ui/static/legal-<name>.html` and open
# through the shared route `/p/legal-<name>`; they deliberately have no route of their own:
# the shared one is already in every build, and a document works as soon as the file is in
# place, not after a rebuild of the `.exe`.
DOCS = ("terms", "privacy")


class Acks:
    """What the person has confirmed on this computer.

    It lives in `argus.legal.json` next to the settings, not inside `argus.local.json`: that
    one is a settings layer with its own key validation, while this is a record of an event.
    And not next to the account sign-in: signing out does not reset it, since the notice about
    recording audio belongs to the computer, not to the person.

    Without a directory (tests build `Config()` without a file) the mark lives in memory.
    """

    def __init__(self, root: Path | None):
        self.path = root / STORE_NAME if root is not None else None
        self._mem: dict = {}
        self._asked: float | None = None

    def _read(self) -> dict:
        if self.path is None:
            return dict(self._mem)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        # A mark that could not be written to disk (disk full, read-only folder, antivirus) lives in
        # memory. Without that nobody would see it: there is no file, "Got it, turn on"
        # changed nothing, and the notice window opened again after every press.
        return {**data, **self._mem}

    def notice_done(self) -> bool:
        return self._read().get("notice") == NOTICE_VERSION

    def ask(self) -> None:
        """Turning on was refused until the notice is shown: the page must show it.

        The hotkey does not hand the answer to the page: `/control` is called by a separate
        thread, and the window knows only what it sees in the state snapshot. The mark lives
        briefly (`asking()`) and is needed only for these cases.
        """
        self._asked = time.monotonic()

    def asking(self, window: float = 20.0) -> bool:
        """Whether a notice was recently requested to be shown and the person has not seen it yet."""
        return (self._asked is not None and time.monotonic() - self._asked < window
                and not self.notice_done())

    def confirm_notice(self) -> None:
        data = {**self._read(), "notice": NOTICE_VERSION}
        if self.path is None:
            self._mem = data
            return
        try:
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except OSError as e:
            # It was not saved, so we will ask again next time. That is inconvenient but honest: better
            # to show the notice twice than to decide for the person that they saw it.
            log.warning("could not save the notification marker: %s", e)
            self._mem = data


def acks_of(app) -> Acks:
    """One mark per app, by the same technique as `account_of`."""
    existing = getattr(app, "_acks", None)
    if existing is None:
        cfg = app.cfg
        root = cfg.root if cfg.config_path is not None else None
        existing = Acks(root)
        app._acks = existing
    return existing
