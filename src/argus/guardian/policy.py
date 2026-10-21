"""What the guardian may read, what it may change, and what a finished fix must look like.

Two layers use this module and neither trusts the other:

* the tools check every path *while the model works* (`check_read`, `check_write`);
* after the model stops, `check_diff` judges the whole change from `git`'s own output, as if a
  stranger had sent it.

The deny lists are deliberately blunt. A guardian that cannot edit the billing code is a guardian
that cannot break the billing code, and the cost of a false "no" is one person reading a pull
request, while the cost of a false "yes" is money.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from argus.guardian.redact import contains_secret


class PolicyError(Exception):
    """A request the policy refuses. The message is safe to show the model."""


# Never shown to the model, never written: secrets, keys, databases, personal data.
READ_DENY = [
    r"(^|/)\.git(/|$)",
    r"(^|/)\.env($|\.)",
    r"\.(key|pem|p12|pfx|cer|crt)$",
    r"\.(sqlite|sqlite3|db)(-wal|-shm)?$",
    r"\.secret$",
    r"(^|/)argus\.(account|local|legal)\.json",
    r"(^|/)(id_rsa|id_ed25519)",
    r"proxy-secret",
    r"(^|/)(chats|sessions|models)(/|$)",
    r"\.log$",
    r"(^|/)seller\.json$",
    r"update-signing",
]

# Code that moves money, identifies people, signs or ships releases, or is the guardian itself.
# Readable (a diagnosis needs it), never changeable by the guardian.
WRITE_DENY = READ_DENY + [
    (r"^src/argus/server/(billing|accounts|entitlements|tokens|relay|relay_stt|pricing|db|limits|throttle|weakpass"
     r"|emails|mailer|erase|export|grant|block|discord_link|discord_bot|cabinet)\.py$"),
    r"^src/argus/server/cabinet_texts/",
    r"^src/argus/(update|update_service|update_trust|ed25519)\.py$",
    r"^src/argus/mac/update\.py$",
    r"^src/argus/account/",
    r"^src/argus/guardian/",
    r"^tests/(test_guardian\w*\.py|conftest\.py)$",
    r"^(deploy|packaging|legal|landing|macos)/",
    r"^\.github/",
    r"^(pyproject\.toml|uv\.lock|argus\.toml|\.gitignore|\.gitattributes|\.dockerignore)$",
    r"(^|/)(Dockerfile|docker-compose[^/]*\.ya?ml)$",
    # Files that Python or pytest run implicitly: whatever is in them executes the moment any test starts.
    r"(^|/)(sitecustomize|usercustomize|conftest)\.py$",
    r"\.pth$",
    r"(^|/)(pytest\.ini|tox\.ini|setup\.cfg|setup\.py|noxfile\.py|MANIFEST\.in|\.coveragerc|Makefile)$",
]

# New files may appear only here, and only as source or notes.
NEW_FILE_ROOTS = ("src/", "tests/")
NEW_FILE_SUFFIXES = (".py", ".md")

# Things a bug fix does not need and a hostile change would: starting processes, opening sockets, leaving the
# interpreter early, or tampering with it. Everything the guardian adds is executed by the test run (tests
# directly, source when imported), so any of these refuses the change. A fix that truly needs one is a fix for a
# person.
RISKY = re.compile(
    r"\b(subprocess|socket|os\.system|os\.popen|os\._exit|os\.kill|os\.fork|os\.exec\w*|sys\.exit|atexit|__import__|"
    r"urllib\.request|requests\.|httpx|shutil\.rmtree|os\.remove|os\.unlink|pickle|ctypes|importlib|multiprocessing|"
    r"__builtins__|builtins\.|sys\.modules|sys\.settrace|eval\(|exec\(|"
    r"from\s+(?:os|sys)\s+import|import\s+(?:os|sys)\s+as|getattr\(\s*(?:os|sys|builtins))"
)
# Worth a reviewer's attention, not worth refusing.
SOFT = re.compile(r"\b(os\.environ|os\.getenv|open\(|write_text|write_bytes)")
# A fix may add tests, but must not make a test easier to pass.
TEST_WEAKENING = re.compile(r"\b(skip|skipif|xfail|importorskip)\b")


@dataclass
class FileChange:
    path: str
    status: str            # git's letter: A, M, D, R, C, T
    added: int = 0
    removed: int = 0
    binary: bool = False


@dataclass
class Violation:
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


def normalize(rel: str) -> str:
    """A repository-relative path in POSIX form, or a PolicyError.

    Absolute paths, drive letters, `..`, and NUL bytes are refused outright; a clever spelling that
    `normpath` would resolve is still a sign that something is probing.
    """
    if not isinstance(rel, str) or not rel.strip():
        raise PolicyError("empty path")
    if "\x00" in rel or len(rel) > 300:
        raise PolicyError("invalid path")
    text = rel.strip().replace("\\", "/")
    if text.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", text):
        raise PolicyError("paths must be relative to the repository")
    parts = text.split("/")
    if ".." in parts:
        raise PolicyError("paths must stay inside the repository")
    for part in parts:
        # Windows treats these spellings as the same file as the plain name, which would slip past the deny lists.
        if ":" in part:
            raise PolicyError("colons are not allowed in paths (NTFS streams)")
        if part not in (".", "") and part != part.rstrip(" ."):
            raise PolicyError("a path component may not end with a dot or a space")
        if re.search(r"~\d", part):
            raise PolicyError("8.3 short names are not allowed")
        if re.match(r"(?i)^(con|prn|aux|nul|com\d|lpt\d)(\.|$)", part):
            raise PolicyError("reserved device names are not allowed")
    norm = posixpath.normpath(text)
    return "" if norm == "." else norm


def _matches(rules: list[str], rel: str) -> bool:
    return any(re.search(rule, rel, re.IGNORECASE) for rule in rules)


class Policy:
    def __init__(self, max_files: int = 6, max_changed_lines: int = 250) -> None:
        self.max_files = max_files
        self.max_changed_lines = max_changed_lines

    # --- single paths ------------------------------------------------------------------

    def check_read(self, rel: str) -> str:
        norm = normalize(rel)
        if _matches(READ_DENY, norm):
            raise PolicyError(f"{norm!r} is protected and cannot be read")
        return norm

    def check_write(self, rel: str) -> str:
        norm = normalize(rel)
        if not norm:
            raise PolicyError("cannot write to the repository root")
        if _matches(WRITE_DENY, norm):
            raise PolicyError(f"{norm!r} is in a protected area and cannot be changed")
        return norm

    def is_write_denied(self, rel: str) -> bool:
        try:
            self.check_write(rel)
        except PolicyError:
            return True
        return False

    def check_new_file(self, rel: str) -> str:
        norm = self.check_write(rel)
        if not norm.startswith(NEW_FILE_ROOTS) or not norm.endswith(NEW_FILE_SUFFIXES):
            raise PolicyError("new files may only be .py or .md files under src/ or tests/")
        return norm

    # --- a finished change -------------------------------------------------------------

    def check_diff(self, changes: list[FileChange], added_lines: dict[str, list[str]]) -> tuple[list[Violation], list[str]]:
        """Judge a change. Returns (violations, risk flags). No violations means the gate is open."""
        bad: list[Violation] = []
        flags: list[str] = []
        if not changes:
            bad.append(Violation("empty", "the change touches no files"))
        if len(changes) > self.max_files:
            bad.append(Violation("too-many-files", f"{len(changes)} files, the limit is {self.max_files}"))
        total = sum(c.added + c.removed for c in changes)
        if total > self.max_changed_lines:
            bad.append(Violation("too-large", f"{total} changed lines, the limit is {self.max_changed_lines}"))
        for c in changes:
            if c.binary:
                bad.append(Violation("binary", f"{c.path} is a binary file"))
            if c.status[:1] in ("D", "R", "C", "T"):
                bad.append(Violation("structure", f"{c.path}: files may be added or edited, not deleted, renamed, or retyped"))
            try:
                if c.status[:1] == "A":
                    self.check_new_file(c.path)
                else:
                    self.check_write(c.path)
            except PolicyError as e:
                bad.append(Violation("protected-path", str(e)))
            if c.path.startswith("tests/") and c.status[:1] == "M" and c.removed:
                bad.append(Violation("test-weakened", f"{c.path}: existing test lines were removed or rewritten"))
        for path, lines in added_lines.items():
            blob = "\n".join(lines)
            if contains_secret(blob):
                bad.append(Violation("secret", f"{path}: added text looks like a credential"))
            if path.startswith("tests/") and TEST_WEAKENING.search(blob):
                bad.append(Violation("test-weakened", f"{path}: added skip/xfail markers"))
            if RISKY.search(blob):
                bad.append(Violation("risky-code", f"{path}: adds {sorted(set(RISKY.findall(blob)))}"))
            if SOFT.search(blob):
                flags.append(f"{path}: touches the environment or the file system ({sorted(set(SOFT.findall(blob)))})")
        return bad, flags
