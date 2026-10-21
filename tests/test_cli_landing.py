"""Where the page opens at launch: the dashboard normally, but the account section until
sign-in to the configured server. Without this, the first screen after download is the
working control panel, where the recognition button silently refuses, and
the reason is written only inside the settings.
"""

from __future__ import annotations

from argus.cli import _landing_path
from argus.core.config import Config


def test_dashboard_when_no_account_server(tmp_path):
    cfg = Config()
    cfg.root = tmp_path
    cfg.account.server = ""

    assert _landing_path(cfg) == ""


def test_account_section_when_signed_out(tmp_path):
    cfg = Config()
    cfg.root = tmp_path
    cfg.account.server = "http://example.invalid"

    # Not `/admin#account`: the server redirects `/admin` to `/#admin` and loses the tab.
    assert _landing_path(cfg) == "/#account"


def test_dashboard_once_signed_in(tmp_path):
    cfg = Config()
    cfg.root = tmp_path
    cfg.account.server = "http://example.invalid"
    (tmp_path / "argus.account.json").write_text(
        '{"refresh": "tok", "email": "user@example.com"}', encoding="utf-8"
    )

    assert _landing_path(cfg) == ""
