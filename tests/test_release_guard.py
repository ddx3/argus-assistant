"""The shared distribution gates (`deploy/release_guard.py`): secrets, terms placeholders, a
build of the wrong flavor, the release number.

None of the gate's checks used to be tested themselves. Here there are fake builds with
planted keys (in text, in a binary file, inside compressed code) and foreign key formats:
exactly what used to get through.
"""

from __future__ import annotations

import sys
import urllib.error
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
sys.path.insert(0, str(DEPLOY))

import release_guard as guard  # noqa: E402

REAL = b"the-real-openai-value-0123456789abcdef"


def secret_free(name: str, data: bytes, secrets=()) -> str:
    return guard.scan_data(name, data, secrets)


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        b'key = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH"',                # OpenAI
        b"x = 'sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789'",                      # Anthropic
        b'ELEVEN = "sk_' + b"0123456789abcdef" * 3 + b'"',                               # ElevenLabs
        b"token ghp_abcdefghijklmnopqrstuvwxyz0123456789",                               # GitHub
        b"MTA0NzM5NTQzMjE5ODc2NTQzMg.GaBcDe." + b"x" * 27,                               # Discord
        b"-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----",      # a private key
        b"AKIAABCDEFGHIJKLMNOP",                                                         # AWS
        b"LIQPAY_PRIVATE_KEY=abcdefghijklmnopqrstuvwxyz123456\n",                        # NAME=value
        b"SMTP_PASSWORD = supersecretpassword12345\n",
    ],
)
def test_keys_of_every_common_format_are_found_in_text(text):
    assert "looks like a key" in secret_free("a.txt", text) or "NAME_KEY" in secret_free("a.txt", text)


@pytest.mark.parametrize(
    "text",
    [b"just a sentence about sk-learn and tokens", b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
     b"password = ''", b"API_KEY=short", b""],
)
def test_ordinary_text_is_not_a_key(text):
    assert secret_free("a.txt", text) == ""


def test_the_real_value_from_env_is_found_anywhere_even_in_binaries():
    blob = b"MZ\x00\x01" + b"\x00" * 50 + REAL + b"\x00" * 50
    assert "real value" in secret_free("Argus.exe", blob, [REAL])
    assert secret_free("Argus.exe", blob, []) == "", "without a value from .env a binary file is not a key by itself"


def test_binaries_are_scanned_for_strict_keys_without_a_size_or_nul_exemption():
    """Before, everything binary and everything larger than 5 MB was skipped, which is almost the
    whole build.
    """
    key = b"sk-proj-" + b"A1b2C3d4" * 6
    assert "looks like a key" in secret_free("x.dll", b"MZ\x00\x00" + b"\x00" * 100 + key)
    assert "looks like a key" in secret_free("big.bin", b"\x00" * 6_000_000 + key)


def test_a_key_inside_compressed_code_is_found():
    """PyInstaller modules lie in the PYZ in zlib chunks: a constant with a key is invisible to a
    plain search.
    """
    module = b"import os\nTOKEN = '" + REAL + b"'\nSK = 'sk-proj-" + b"Zz9Yy8Xx" * 6 + b"'\n"
    blob = b"MZ\x90\x00" + b"\x00" * 400 + b"junk\x78\x9cnot-a-stream" + zlib.compress(module) + b"\x00" * 200
    assert "compressed code" in secret_free("Argus.exe", blob, [REAL])
    assert "compressed code" in secret_free("Argus.exe", blob, [])      # and by the shape of a key, without a value from .env
    clean = b"MZ\x90\x00" + zlib.compress(b"import os\nprint('hello world, nothing here')\n" * 5)
    assert secret_free("Argus.exe", clean, [REAL]) == ""


def test_library_constants_in_compressed_code_are_not_keys():
    """Verified on the compressed code of the environment's packages: Pillow carries a font in
    base64 ("AKIA…" occurs in it on its own), and `cryptography` carries the header "BEGIN
    OPENSSH PRIVATE KEY" as a constant. Before, both stopped every publication (in `Argus.exe`,
    the whole code)."""
    module = (b"FONT = 'AAEAAAAKIAAQAAAAAABAAHAM0AAQAAAAAABQA8AU8AAQAAAAAABgAPAawAAQAAAAAACAALAdQAAQ'\n"
              b"_START = b'-----BEGIN OPENSSH PRIVATE KEY-----'\n_END = b'-----END OPENSSH PRIVATE KEY-----'\n" * 3)
    blob = b"MZ\x90\x00" + b"\x00" * 300 + zlib.compress(module) + b"\x00" * 100
    assert secret_free("Argus.exe", blob, [REAL]) == ""


def test_a_private_key_with_a_body_is_found_in_a_binary_and_a_bare_aws_id_in_text():
    body = b"MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj\n" * 3
    pem = b"-----BEGIN PRIVATE KEY-----\n" + body + b"-----END PRIVATE KEY-----\n"
    assert "looks like a key" in secret_free("x.dll", b"MZ\x00\x00" + b"\x00" * 100 + pem)
    blob = b"MZ\x90\x00" + b"\x00" * 300 + zlib.compress(b"KEY = '''" + pem + b"'''\n")
    assert "compressed code" in secret_free("Argus.exe", blob, [])
    assert secret_free("a.css", b"src: url(data:font/ttf;base64,AAAAAAAAAKIAAQAAAAAABAAHAM0AAQAAAAAABQA8AU8AAQAA)") == ""
    assert "looks like a key" in secret_free("a.txt", b"aws_access_key_id = AKIAABCDEFGHIJKLMNOP\n")


def test_a_name_on_the_allow_list_is_skipped(monkeypatch):
    monkeypatch.setattr(guard, "ALLOWED_FILES", ("docs/example.md",))
    assert secret_free("docs/example.md", b"sk-proj-" + b"A1b2C3d4" * 6) == ""


def test_a_huge_file_is_read_in_blocks_not_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "_WHOLE_FILE_LIMIT", 1000)
    monkeypatch.setattr(guard, "_BLOCK", 600)
    path = tmp_path / "huge.bin"
    path.write_bytes(b"\x00" * 1500 + REAL + b"\x00" * 1500)
    assert "real value" in guard.scan_file(path, "huge.bin", [REAL])
    split = tmp_path / "split.bin"
    cut = 600 * 3 - 10                                     # the value is split by a block boundary
    split.write_bytes(b"\x00" * cut + REAL + b"\x00" * 2000)
    assert "real value" in guard.scan_file(split, "split.bin", [REAL])


def test_the_tree_scan_reports_the_first_problem_and_counts_files(tmp_path):
    (tmp_path / "ok.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "bad.txt").write_bytes(b"key sk-proj-" + b"A1b2C3d4" * 6)
    scanned, problem = guard.scan_tree(tmp_path)
    assert scanned == 2 and "sub/bad.txt" in problem


def test_secrets_are_read_from_the_projects_env_without_being_printed(tmp_path):
    (tmp_path / ".env").write_text(
        "# comment\nOPENAI_API_KEY='sk-aaaaaaaaaaaaaaaa'\nSHORT=abc\nEMPTY=\nNAME=value-long-enough-12\n", encoding="utf-8")
    nowhere = tmp_path / "no-personal-folder"
    assert guard.secrets_from_env(tmp_path, nowhere) == [b"sk-aaaaaaaaaaaaaaaa", b"value-long-enough-12"]
    assert guard.secrets_from_env(tmp_path / "none", nowhere) == []


def test_the_servers_env_and_the_personal_folder_count_as_secrets_too(tmp_path):
    """Before, only the root `.env` was read: a value from `deploy/server/.env` (OpenAI keys, the
    payment provider, mail) or from `~/.argus` (release signing keys) passed the check in a
    build."""
    (tmp_path / "deploy" / "server").mkdir(parents=True)
    (tmp_path / "deploy" / "server" / ".env").write_text("ARGUS_SMTP_PASSWORD=smtp-password-0123456789\n", encoding="utf-8")
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / "update-signing.key").write_text("signing-key-material-0123456789\n", encoding="utf-8")
    (personal / "resend_api_key").write_text("re_0123456789abcdef0123\n", encoding="utf-8")
    (personal / "seller.json").write_text('{"name": "seller-json-is-not-a-secret-0123456789"}', encoding="utf-8")
    (personal / "macos-signing.p12").write_bytes(bytes(2) + b"binary-certificate-0123456789")
    found = guard.secrets_from_env(tmp_path, personal)
    assert b"smtp-password-0123456789" in found
    assert b"signing-key-material-0123456789" in found and b"re_0123456789abcdef0123" in found
    assert not any(b"seller" in v or b"certificate" in v for v in found)
    assert "real value" in guard.scan_data("app/x.py", b"x = 'smtp-password-0123456789'", found)


# --------------------------------------------------------------------------
# Terms placeholders
# --------------------------------------------------------------------------


def test_placeholder_marks_in_the_legal_pages_are_found():
    pages = [
        ("terms.html", b'<p>Write to <mark class="todo">[email]</mark></p>'),
        ("privacy.html", b'<a href="mailto:TODO-email">mail</a>'),
        ("help.html", b"<p>all good</p>"),
    ]
    problems = guard.legal_problems(pages)
    assert len(problems) == 2 and "terms.html" in problems[0] and "privacy.html" in problems[1]
    assert guard.legal_problems([("help.html", b"<p>fine</p>")]) == []
    assert "--allow-legal-placeholders" in guard.legal_message(problems)


# --------------------------------------------------------------------------
# A build of the wrong flavor
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["_internal/claude_agent_sdk/_bundled/claude.exe", "claude.exe", "Argus.app/Contents/Frameworks/claude_agent_sdk/x.py",
     "_internal/ctranslate2/ctranslate2.dll", "_internal/faster_whisper/__init__.py"],
)
def test_a_build_of_the_wrong_flavor_is_refused(path):
    assert "wrong build flavor" in guard.flavor_problem(["Argus.exe", path])


def test_the_right_flavor_passes():
    names = ["Argus.exe", "_internal/anthropic/__init__.py", "_internal/claude_agent_sdk-0.1.dist-info/METADATA"]
    assert guard.flavor_problem(names) == ""


# --------------------------------------------------------------------------
# Release number
# --------------------------------------------------------------------------


def test_the_build_number_never_goes_below_the_published_one():
    assert guard.next_build(None, now=1000.0) == 1000
    assert guard.next_build({"build": 500}, now=1000.0) == 1000
    assert guard.next_build({"build": 5000}, now=1000.0) == 5001, "the clock is behind the published one: still greater than the published one"
    assert guard.next_build({"build": True}, now=1000.0) == 1000 and guard.next_build({"build": "9"}, now=1000.0) == 1000


def test_a_clock_that_disagrees_with_the_servers_is_refused():
    """A release "from the future" would make all the apps ignore the next, correctly numbered
    ones.
    """
    assert guard.next_build(None, now=10_000.0, server_now=10_300.0) == 10_000
    with pytest.raises(SystemExit, match="clock"):
        guard.next_build(None, now=10_000.0, server_now=10_000.0 - 3 * 3600)
    with pytest.raises(SystemExit, match="clock"):
        guard.next_build(None, now=10_000.0 + 3 * 3600, server_now=10_000.0)


def test_the_live_release_is_read_with_the_servers_clock():
    class Response:
        headers = {"Date": "Wed, 14 Oct 2026 10:00:00 GMT"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b'{"version": "1.0+x", "build": 77}'

    info, now = guard.fetch_live("http://example.invalid/latest.json", opener=lambda url, timeout: Response())
    assert info == {"version": "1.0+x", "build": 77} and now == pytest.approx(1_791_972_000.0, abs=1)


def test_no_live_release_yet_is_not_an_error_but_a_dead_server_is():
    def missing(url, timeout):
        raise urllib.error.HTTPError(url, 404, "not found", SimpleNamespace(get=lambda k: None), None)

    assert guard.fetch_live("http://example.invalid/latest.json", opener=missing)[0] is None

    def dead(url, timeout):
        raise urllib.error.URLError("no connection")

    with pytest.raises(SystemExit, match="released version"):
        guard.fetch_live("http://example.invalid/latest.json", opener=dead)
