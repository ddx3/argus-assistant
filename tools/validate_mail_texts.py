"""Checking the translations of the server emails: `src/argus/server/mail_texts/<language>.json`
against the English original.

    python tools/validate_mail_texts.py            # all languages
    python tools/validate_mail_texts.py de pl      # only these

What gets checked is what breaks an email silently: different keys, a lost placeholder
(`{email}`, `{amount}`…), a different number of paragraphs and lines, not 12 months,
leftovers of English, the wrong script. Only a human can check the meaning.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parents[1] / "src" / "argus" / "server" / "mail_texts"
LANGS = ("ru", "uk", "es", "pt", "de", "fr", "tr", "pl")        # English is the original
CYRILLIC = {"ru", "uk"}
SLOT = re.compile(r"\{(\w+)\}")
ENGLISH_WORDS = {"the", "and", "you", "your", "our", "that", "this", "with", "can", "may", "from", "have",
                 "payment", "account", "write", "password"}


def strings(node, path=""):
    """All JSON strings with their paths (`events.reset.paragraphs[0]`)."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from strings(item, f"{path}[{i}]")
    elif isinstance(node, dict):
        for key, item in node.items():
            yield from strings(item, f"{path}.{key}" if path else key)


def check(lang: str) -> list[str]:
    problems = []
    path = DIR / f"{lang}.json"
    try:
        tr = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"{lang}: file cannot be read as JSON: {e}"]
    en = json.loads((DIR / "en.json").read_text(encoding="utf-8"))
    a, b = dict(strings(en)), dict(strings(tr))
    if set(a) != set(b):
        missing = sorted(set(a) - set(b))
        extra = sorted(set(b) - set(a))
        problems.append(f"{lang}: different keys; missing: {missing[:6]}, extra: {extra[:6]}")
    for key in sorted(set(a) & set(b)):
        if key.startswith("date.months"):
            continue
        pa, pb = sorted(SLOT.findall(a[key])), sorted(SLOT.findall(b[key]))
        if pa != pb:
            problems.append(f"{lang}: {key}: placeholders {pb}, need {pa}")
    if len(tr.get("date", {}).get("months", [])) != 12:
        problems.append(f"{lang}: date.months must have 12 names")
    fmt = tr.get("date", {}).get("format", "")
    for slot in ("{d}", "{m}", "{y}", "{time}"):
        if slot not in fmt:
            problems.append(f"{lang}: date.format has no {slot}")
    text = " ".join(v for k, v in b.items() if not k.startswith("date.months"))
    text_no_slots = SLOT.sub("", text)
    words = re.findall(r"[A-Za-z']+", text_no_slots)
    english = [w for w in words if w.lower() in ENGLISH_WORDS]
    if len(english) > 2:
        problems.append(f"{lang}: English leftovers ({len(english)}): {' '.join(english[:8])}")
    has_cyr = bool(re.search("[А-Яа-яІіЇїЄєҐґ]", text))
    if lang in CYRILLIC and not has_cyr:
        problems.append(f"{lang}: no Cyrillic")
    if lang not in CYRILLIC and has_cyr:
        problems.append(f"{lang}: there is Cyrillic")
    return problems


def main(argv: list[str]) -> int:
    langs = argv or list(LANGS)
    bad = 0
    for lang in langs:
        problems = check(lang)
        print(f"{lang}: {'ok' if not problems else str(len(problems)) + ' issues'}")
        for p in problems:
            print("   -", p)
        bad += len(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
