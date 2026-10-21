"""Reading `.env` and settings: foreign encodings, typos, the dashboard address.

The public build ships without `.env`, and people create it by hand: PowerShell 5.1,
old Notepad, copy-paste from an email. This checks what used to break silently: the key
was not picked up, or startup crashed on the encoding.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus.core.config import Config, UIConfig, load_env, unknown_keys

NAMES = ("ARGUS_T_KEY", "ARGUS_T_SECOND", "ARGUS_T_THIRD")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """`load_env` writes into the real environment — an empty value restores everything as it
    was.
    """
    for name in NAMES:
        monkeypatch.setenv(name, "")


def env_file(tmp_path, data: bytes):
    path = tmp_path / ".env"
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------


def test_a_plain_file_works_as_before(tmp_path):
    taken = load_env(env_file(tmp_path, (
        b"# comment\n\nARGUS_T_KEY=abc\nARGUS_T_SECOND = \"quoted\"\nARGUS_T_THIRD='single'\n")))
    assert taken == {"ARGUS_T_KEY": "abc", "ARGUS_T_SECOND": "quoted", "ARGUS_T_THIRD": "single"}


def test_a_bom_does_not_eat_the_first_key(tmp_path):
    """PowerShell 5.1 `Out-File -Encoding utf8` and the old Notepad: the BOM stuck to the name."""
    taken = load_env(env_file(tmp_path, b"\xef\xbb\xbfARGUS_T_KEY=abc\n"))
    assert taken == {"ARGUS_T_KEY": "abc"}


def test_a_utf16_file_is_read(tmp_path):
    """PowerShell 5.1 `>` writes UTF-16 — launch crashed on the encoding."""
    taken = load_env(env_file(tmp_path, "ARGUS_T_KEY=abc\n# note\n".encode("utf-16")))
    assert taken == {"ARGUS_T_KEY": "abc"}


def test_an_ansi_file_with_a_russian_comment_still_gives_its_keys(tmp_path):
    # language data: cp1251 can only hold Cyrillic, so the comment stays Russian ("my key")
    taken = load_env(env_file(tmp_path, "# мой ключ\nARGUS_T_KEY=abc\n".encode("cp1251")))
    assert taken == {"ARGUS_T_KEY": "abc"}


def test_a_file_in_the_wrong_encoding_does_not_stop_the_start(tmp_path):
    """UTF-16 without a BOM is read as UTF-8 with NULs in the names — the environment does not
    take them, but does not crash either.
    """
    taken = load_env(env_file(tmp_path, "ARGUS_T_KEY=abc\n".encode("utf-16-le")))
    assert taken == {}


def test_an_unreadable_file_does_not_stop_the_start(tmp_path, monkeypatch):
    path = env_file(tmp_path, b"ARGUS_T_KEY=abc\n")
    real = type(path).read_bytes

    def locked(self):
        if self.name == ".env":
            raise PermissionError(13, "in use")
        return real(self)

    monkeypatch.setattr(type(path), "read_bytes", locked)
    assert load_env(path) == {}


def test_export_and_inline_comments_are_understood(tmp_path):
    taken = load_env(env_file(tmp_path, (
        b"export ARGUS_T_KEY=abc\n"
        b'ARGUS_T_SECOND="two" # my note\n'
        b"ARGUS_T_THIRD=three # note\n")))
    assert taken == {"ARGUS_T_KEY": "abc", "ARGUS_T_SECOND": "two", "ARGUS_T_THIRD": "three"}


def test_a_hash_inside_a_value_stays(tmp_path):
    taken = load_env(env_file(tmp_path, b"ARGUS_T_KEY=ab#cd\nARGUS_T_SECOND='x # y'\n"))
    assert taken == {"ARGUS_T_KEY": "ab#cd", "ARGUS_T_SECOND": "x # y"}


def test_the_environment_wins_over_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_T_KEY", "from-terminal")
    assert load_env(env_file(tmp_path, b"ARGUS_T_KEY=from-file\n")) == {}


def test_skipped_names_are_not_exported(tmp_path):
    import os

    taken = load_env(env_file(tmp_path, b"ARGUS_T_KEY=abc\nARGUS_T_SECOND=two\n"),
                     skip=("ARGUS_T_KEY",))
    assert taken == {"ARGUS_T_SECOND": "two"}
    assert not os.environ.get("ARGUS_T_KEY")


def test_the_subscription_provider_does_not_inherit_the_api_key(tmp_path, monkeypatch):
    """The child process of the `claude-code` provider inherits the environment and would
    prefer the key to the subscription.
    """
    import os

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    (tmp_path / "argus.toml").write_text('[brain]\nprovider = "claude-code"\n', encoding="utf-8")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-real\nARGUS_T_KEY=abc\n",
                                   encoding="utf-8")
    Config.load(tmp_path / "argus.toml")
    assert not os.environ.get("ANTHROPIC_API_KEY")
    assert os.environ.get("ARGUS_T_KEY") == "abc"


def test_other_providers_get_their_key(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    (tmp_path / "argus.toml").write_text('[brain]\nprovider = "anthropic-api"\n', encoding="utf-8")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-real\n", encoding="utf-8")
    Config.load(tmp_path / "argus.toml")
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-real"


# --------------------------------------------------------------------------
# Dashboard address
# --------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1", "localhost", "LocalHost"])
def test_loopback_hosts_are_accepted(host):
    assert UIConfig(host=host).host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "example.com", ""])
def test_anything_else_is_refused(host):
    """A dashboard without a password shows the screen and the conversation — it must not go
    out to the network.
    """
    with pytest.raises(ValidationError):
        UIConfig(host=host)


def test_a_wildcard_host_in_the_local_file_is_dropped_not_obeyed(tmp_path):
    (tmp_path / "argus.toml").write_text("", encoding="utf-8")
    (tmp_path / "argus.local.json").write_text('{"ui": {"host": "0.0.0.0"}}', encoding="utf-8")
    assert Config.load(tmp_path / "argus.toml").ui.host == "127.0.0.1"


# --------------------------------------------------------------------------
# Typos in the settings
# --------------------------------------------------------------------------


def test_misspelled_keys_are_listed_by_their_full_path():
    raw = {"gate_brain": {"cooldown": 3}, "vadd": {"threshold": 0.9},
           "ui": {"prot": 1, "port": 8421}}
    assert sorted(unknown_keys(raw)) == ["gate_brain.cooldown", "ui.prot", "vadd"]


def test_known_keys_and_the_free_channel_section_are_not_reported():
    raw = {"stt": {"language": "ru"}, "channel": {"anything": {"goes": 1}}}
    assert unknown_keys(raw) == []


def test_a_renamed_legacy_key_is_not_a_typo():
    assert unknown_keys({"audio": {"loopback_window": "Zoom"}}) == []


def test_the_project_file_has_no_unknown_keys():
    import tomllib
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "argus.toml"
    with path.open("rb") as f:
        assert unknown_keys(tomllib.load(f)) == []


def test_a_typo_is_reported_when_the_config_is_loaded(tmp_path, caplog):
    (tmp_path / "argus.toml").write_text("[gate_brain]\ncooldown = 3\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        cfg = Config.load(tmp_path / "argus.toml")
    assert cfg.gate_brain.cooldown_s == 8.0                 # the typo still has no effect
    assert "gate_brain.cooldown" in caplog.text


# --------------------------------------------------------------------------
# Core defaults
# --------------------------------------------------------------------------


def test_the_default_model_is_one_the_relay_sells():
    """Without `argus.toml` the defaults must not ask the relay for what it does not sell."""
    from argus.server import pricing

    brain = Config().brain
    assert brain.api_model in pricing.MODELS
    assert pricing.MODELS[brain.api_model].provider == brain.relay_upstream
