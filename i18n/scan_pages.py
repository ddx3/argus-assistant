"""English text of pages and server messages that has no translation key.

    python i18n/scan_pages.py            shows such strings (exit code 1 if there are any)

The page engine (`ui/static/i18n/engine.js`) translates the ready text of a node as a whole:
an exact match with a key, or a key with `{places}`; markup with tags is compared as a whole.
This does the same parsing, but over the sources: from pages it takes code strings, text
between tags and labels (`placeholder`, `aria-label`, `alt`), and from Python the texts of
refusals (`HTTPException`, `Denied`, `{"error": …}` and the like). Strings for the log and
the browser console (`console.warn(...)`, `new Error(...)`, `log.warning(...)`) are not seen
by a person and are not checked.

Why: a new English string without an entry in `i18n/keys_manual.py` stays English in all
eight languages, and you can only notice it by eye in a foreign language. The check is
`tests/test_i18n_coverage.py`.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "argus" / "ui" / "static"
SRC = ROOT / "src" / "argus"
PAGES = ("index.html", "home.html", "record.html", "admin.html", "account.js", "chrome.js", "admin.js",
         "winbar.js", "update-button.js")
LATIN = re.compile("[A-Za-z]")
LETTERS = re.compile("[A-Za-z]{2}")
# Not a sentence: an identifier, a selector, a path, a type: no spaces and nothing but lower-case letters and signs.
TECHNICAL = re.compile(r"^[a-z0-9_\-:./#\[\]=*,>+%@(){}!$&?|\\^~`'\"<;]+$")
# Strings of the page code that are not text for a person although they look like it (headers, key names, ...).
NOT_TEXT = {"Argus", "· Argus", "@@INLINE", "button, a, img, input", "use strict", "SELECT", "PATCH", "Files", "Home", "End", "Key", "Digit", "AltGraph",
            "NotAllowedError", "Content-Type", "POST", "GET", "PUT", "DELETE", "Escape", "Enter", "Tab", "Shift", "Ctrl", "Alt", "Meta",
            "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Backspace", "OK", "UTF-8"}
# Python calls and fields whose text reaches a person (the page shows it as is).
RAISERS = {"HTTPException", "Denied", "Busy", "AccountError", "RecordError", "ChatEditError", "_abandon"}
MESSAGE_KEYS = {"error", "detail"}
# Functions that consist entirely of text for a person: complaints in the problems window
# (`Argus.problems`, the `complaints` of sound and recognition, the `problem` of voice-over)
# and the replies of the "Invisible on recording" switch.
MESSAGE_FUNCS = {"complaints", "problems", "_hotkey_problems", "problem", "hide_from_capture", "show_in_capture"}
LOG_BEFORE = re.compile(r"(console\s*\.\s*\w+|new\s+Error|\bError)\s*\(\s*$")


def norm(s: str) -> str:
    return re.sub(r"[\s  ]+", " ", s).strip()


def is_text(s: str) -> bool:
    """A string a person may read: it has words in it, and it is not an identifier, a selector or a path."""
    s = norm(s.replace("\1", " ").replace("{x}", " "))
    if s in NOT_TEXT or not LETTERS.search(s):
        return False
    return " " in s or not TECHNICAL.match(s)


# Characters that are never in a sentence but are everywhere in selectors, attribute lists, paths and CSS.
CODE_CHARS = re.compile(r"[#\[\]=<>\{}]|://|^[./]|[a-z]\.[a-z]|\s\.[a-z]")
LOWER_WORDS = re.compile(r"[a-z][a-z0-9\-]*(?:,? [a-z][a-z0-9\-]*){0,2}")


def is_code_text(s: str, strict: bool = True) -> bool:
    """`is_text` for a string of page code, which is full of selectors and class lists. `strict=False` is for
    pieces cut out of markup (between tags), which are text for sure; they only must not hold half of a tag."""
    if not is_text(s):
        return False
    t = norm(s.replace("", " ").replace("{x}", " "))
    if re.search(r"[<>=#\[\]\{}]", t):
        return False
    if not re.search("[A-Za-z]{3}", t):                 # "0px", "22px": a number with a unit
        return False
    return not strict or not (CODE_CHARS.search(t) or LOWER_WORDS.fullmatch(t))


class Keys:
    """The same lookup rules as in the engine: exact key, key with `{slots}`, key with tags."""

    def __init__(self) -> None:
        raw = json.loads((STATIC / "i18n" / "keys.json").read_text(encoding="utf-8"))["keys"]
        self.exact: set[str] = set()
        self.patterns: list[re.Pattern[str]] = []
        self.html_exact: set[str] = set()
        self.html_patterns: list[re.Pattern[str]] = []
        for e in raw:
            k = norm(e["k"])
            slot = re.search(r"\{[a-z_]+\}", k) is not None
            html = "<" in k
            if slot:
                rx = re.sub(r"\\\{[a-z_]+\\\}", "(.*?)", re.escape(k))
                (self.html_patterns if html else self.patterns).append(re.compile("^" + rx + "$"))
            elif html:
                self.html_exact.add(k)
            else:
                self.exact.add(k)
        # Pieces of keys between places and tags: a piece that the code glues together from parts is
        # looked up among them.
        self.segments = {norm(s) for e in raw for s in re.split(r"\{[a-z_]+\}|<[^>]*>", e["k"]) if LATIN.search(s)}

    def has(self, text: str, html: bool = False) -> bool:
        s = norm(text)
        if html:
            return s in self.html_exact or any(p.match(s) for p in self.html_patterns)
        return s in self.exact or any(p.match(s) for p in self.patterns)

    def covers(self, frag: str) -> bool:
        """A piece of page text (`{x}` is a substituted value) is translated.

        Besides an exact match: a piece with a "glued-on" value ("Waiting for payment{x}") matches by
        the text without it; a piece of a string that the code assembles from parts ("No answers yet",
        plus " — recognition is off") matches if it is the tail or the start of some key (ten
        characters or more: anything shorter would match everything)."""
        f = norm(frag)
        bare = norm(re.sub(r"^\{x\}|\{x\}$", "", f))
        if self.has(f.replace("{x}", "0")) or (bare != f and self.has(bare)):
            return True
        if len(bare) < 10:
            return False
        for e in self.segments:
            if e.startswith(bare) or e.endswith(bare):
                return True
        return False


# ---------------------------------------------------------------- JavaScript

def _skip_expr(src: str, i: int) -> int:
    """`i` is the position after `${`; return the position after the matching `}` (strings and
    nested templates are not counted).
    """
    depth = 1
    n = len(src)
    while i < n and depth:
        c = src[i]
        if c in "\"'":
            i += 1
            while i < n and src[i] != c:
                i += 2 if src[i] == "\\" else 1
        elif c == "`":
            i = _skip_template(src, i + 1)
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return i


def _skip_template(src: str, i: int) -> int:
    """`i` is the position after the opening backtick; return the position after the closing one."""
    n = len(src)
    while i < n and src[i] != "`":
        if src[i] == "\\":
            i += 2
        elif src.startswith("${", i):
            i = _skip_expr(src, i + 2)
        else:
            i += 1
    return i + 1


def js_tokens(src: str, line: int = 1) -> list[tuple[str, str, int, bool]]:
    """Code strings: (kind, text, line, in_log). The kind is `str`, `tpl` or `op` (the "+" sign
    between strings).
    """
    out: list[tuple[str, str, int, bool]] = []
    i, n = 0, len(src)
    prev = ""

    def regex_ok() -> bool:
        return prev == "" or prev in "(,=:[!&|?{};+-*%<>~^"

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n - 2 if j < 0 else j
            line += src.count("\n", i, j + 2)
            i = j + 2
            continue
        if c in "\"'":
            j, buf = i + 1, []
            while j < n and src[j] != c and src[j] != "\n":
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                buf.append(src[j])
                j += 1
            out.append(("str", unesc("".join(buf)), line, bool(LOG_BEFORE.search(src[max(0, i - 40):i]))))
            i, prev = j + 1, c
            continue
        if c == "`":
            start_line, j, buf, inner = line, i + 1, [], []
            while j < n and src[j] != "`":
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j:j + 2])
                    j += 2
                elif src.startswith("${", j):
                    k = _skip_expr(src, j + 2)
                    inner.append((src[j + 2:k - 1], line))
                    buf.append("\1")
                    line += src.count("\n", j, k)
                    j = k
                else:
                    if src[j] == "\n":
                        line += 1
                    buf.append(src[j])
                    j += 1
            out.append(("tpl", unesc("".join(buf)), start_line,
                        bool(LOG_BEFORE.search(src[max(0, i - 40):i]))))
            for code, ln in inner:           # strings inside `${ … }` are code strings just like any other
                out.extend(js_tokens(code, ln))
            i, prev = j + 1, "`"
            continue
        if c == "/" and regex_ok():          # a regular expression literal
            j, cls = i + 1, False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    cls = True
                elif ch == "]":
                    cls = False
                elif (ch == "/" and not cls) or ch == "\n":
                    break
                j += 1
            i = j + 1
            while i < n and src[i].isalpha():
                i += 1
            prev = ")"
            continue
        if c == "+":
            out.append(("op", "+", line, False))
        elif not c.isspace() and (not out or out[-1][0] != "x"):
            out.append(("x", "", line, False))       # something other than a string: strings must not be glued across it
        prev = c
        i += 1
    return out


def unesc(s: str) -> str:
    return (s.replace("\\'", "'").replace('\\"', '"').replace("\\n", " ").replace("\\u2026", "…")
            .replace("\\u00a0", " ").replace("\\`", "`").replace("\\\\", "\\"))


def joined(tokens: list[tuple[str, str, int, bool]]) -> list[tuple[str, int, bool]]:
    """Strings joined with the "+" sign (`"a " + "b"`) are one string: that is how the page will see
    it.
    """
    res: list[list] = []
    i = 0
    while i < len(tokens):
        kind, text, ln, log = tokens[i]
        if kind in ("op", "x"):
            i += 1
            continue
        item = [text, ln, log]
        while i + 2 < len(tokens) and tokens[i + 1][0] == "op" and tokens[i + 2][0] in ("str", "tpl"):
            item[0] += tokens[i + 2][1]
            i += 2
        res.append(item)
        i += 1
    return [(t, ln, lg) for t, ln, lg in res]


# --------------------------------------------------------------------- HTML

def html_parts(src: str) -> tuple[str, list[tuple[str, int]]]:
    scripts: list[tuple[str, int]] = []

    def grab(m: re.Match[str]) -> str:
        scripts.append((m.group(1), src.count("\n", 0, m.start(1)) + 1))
        return "<script></script>"

    body = re.sub(r"<script[^>]*>(.*?)</script>", grab, src, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"<style[^>]*>.*?</style>", "<style></style>", body, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    return body, scripts


def fragments(text: str, code: bool = False) -> list[tuple[str, str]]:
    """What the engine will see: labels in tags and the text between tags and `${…}` (a value slot is
    `{x}`).
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)      # CSS inside a code string
    out: list[tuple[str, str]] = []
    for tag in re.finditer(r"<[^>]*>", text):
        for a in re.finditer(r"""\b(placeholder|aria-label|title|alt)\s*=\s*"([^"]*)\"""", tag.group(0)):
            out.append(("attr:" + a.group(1), a.group(2)))
    for part in re.split(r"<[^>]*>", text):
        out.append(("text", part))
    res = []
    for kind, p in out:
        p = norm(p.replace("\1", "{x}").replace("&nbsp;", " ").replace("&amp;", "&"))
        if p and (is_code_text(p, strict="<" not in text) if code else is_text(p)):
            res.append((kind, p))
    return res


def page_literals(path: Path) -> list[tuple[int, str, bool, bool]]:
    """(line, text, in_log, in_code): every piece of English text on the page."""
    src = path.read_text(encoding="utf-8", errors="replace")
    res: list[tuple[int, str, bool, bool]] = []
    if path.suffix == ".html":
        body, scripts = html_parts(src)
        for m in re.finditer(r"""\b(?:placeholder|aria-label|title|alt)\s*=\s*"([^"]*)\"|>([^<]+)<""", body):
            text = m.group(1) if m.group(1) is not None else m.group(2)
            if text and is_text(text):
                res.append((0, text, False, False))
        for code, ln in scripts:
            res += [(l, t, lg, True) for t, l, lg in joined(js_tokens(code, ln)) if is_text(t)]
    else:
        res += [(l, t, lg, True) for t, l, lg in joined(js_tokens(src)) if is_text(t)]
    return res


def uncovered_page(name: str, keys: Keys) -> list[tuple[int, str]]:
    out, seen = [], set()
    for ln, text, log, code in page_literals(STATIC / name):
        if log:
            continue
        # The engine compares text with tags against the key as a whole (`htmlElement`), so the whole
        # text is tried first.
        whole = norm(text.replace("\1", "0"))
        if "<" in whole and keys.has(whole, html=True):
            continue
        for kind, frag in fragments(text, code):
            if keys.covers(frag) or frag in seen:
                continue
            seen.add(frag)
            out.append((ln, frag))
    return out


# ------------------------------------------------------------------- Python

def _text_of(node: ast.AST) -> str | None:
    """A string or f-string; a value slot is `{x}`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{x}" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):     # "a" + name + "b" is one string with a value
        left, right = _text_of(node.left), _text_of(node.right)
        if left is not None or right is not None:
            return (left if left is not None else "{x}") + (right if right is not None else "{x}")
    return None


def python_messages(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[int, str]] = []

    def add(node: ast.AST) -> None:
        # "a" if x else "b" gives both variants; adding strings is gluing.
        if isinstance(node, ast.IfExp):
            add(node.body)
            add(node.orelse)
            return
        t = _text_of(node)
        if t and is_text(t) and not re.search(r"%[sdr]", t):     # `%s` marks a string for the log, a person does not see it
            out.append((node.lineno, t))

    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in RAISERS:
                for a in n.args:
                    add(a)
            for kw in n.keywords:
                if kw.arg in MESSAGE_KEYS:
                    add(kw.value)
        elif isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value in MESSAGE_KEYS and v is not None:
                    add(v)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in MESSAGE_FUNCS:
            doc = n.body[0].value if n.body and isinstance(n.body[0], ast.Expr) else None
            # Parts of an f-string and of summands are not taken separately: they are part of the whole
            # string, and that is what we look for.
            parts = {id(v) for j in ast.walk(n) if isinstance(j, (ast.JoinedStr, ast.BinOp)) for v in
                     (j.values if isinstance(j, ast.JoinedStr) else [j.left, j.right])}
            for sub in ast.walk(n):
                if sub is not doc and id(sub) not in parts and isinstance(sub, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                    add(sub)
    return out


def uncovered_python(keys: Keys, files: list[Path] | None = None) -> list[tuple[str, int, str]]:
    res = []
    for p in files or sorted(SRC.rglob("*.py")):
        try:
            found = python_messages(p)
        except SyntaxError:
            continue
        for ln, text in found:
            whole = norm(text.replace("{x}", "0"))
            if keys.has(whole):
                continue
            if fragments(text) and all(keys.covers(f) for _k, f in fragments(text)):
                continue
            res.append((p.relative_to(SRC).as_posix(), ln, norm(text)))
    return res


def main() -> int:
    keys = Keys()
    bad = 0
    for name in PAGES:
        for ln, frag in uncovered_page(name, keys):
            print(f"{name}:{ln}: {frag[:200]}")
            bad += 1
    for rel, ln, text in uncovered_python(keys):
        print(f"{rel}:{ln}: {text[:200]}")
        bad += 1
    print(f"without a key: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
