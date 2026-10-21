"""Checks the translation of the terms and the policy: the structure is the same as in the
English original, nothing is lost.

    python legal/validate_translation.py ru            # one language
    python legal/validate_translation.py               # all with a legal/text/<lang>/ folder

The original is `legal/text/terms.html` and `privacy.html` (English). The translation lies in
`legal/text/<lang>/` under the same names. What is checked is what silently breaks a
document: different markup (a lost paragraph, an extra list, a replaced link), lost numbers
and terms (14 days, $5, 18 years), leftover operator substitutions, leftover English, and the
wrong script. The meaning of a translation cannot be checked — only a human can; this is a
sieve for mechanical errors.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXT = HERE / "text"
DOCS = ("terms", "privacy")
LANGS = ("ru", "uk", "es", "pt", "de", "fr", "tr", "pl")
CYRILLIC = {"ru", "uk"}

TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*)>")
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
# Function English words: they have no way to appear in a translation (deliberately without
# "will", "not", "are": these occur in German and others).
ENGLISH_WORDS = {"the", "and", "you", "your", "our", "that", "this", "with", "can", "may", "from", "have"}
# Substitutions that are forbidden in a translation: the operator and the law are written
# directly, in the needed case and gender, and are not inserted as an English phrase into a
# foreign sentence.
FORBIDDEN_IN_TRANSLATION = ("name", "law", "country")


def skeleton(html: str) -> list[tuple[str, str]]:
    """Sequence of tags with `class` and `href` attributes (nothing else occurs in these documents)."""
    out = []
    for closing, name, attrs in TAG.findall(html):
        keep = re.findall(r'\b(class|href)="([^"]*)"', attrs)
        out.append((("/" if closing else "") + name.lower(), ";".join(f"{k}={v}" for k, v in keep)))
    return out


def visible_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def check(lang: str) -> list[str]:
    problems = []
    folder = TEXT / lang
    for doc in DOCS:
        path = folder / f"{doc}.html"
        if not path.is_file():
            problems.append(f"{lang}/{doc}: file is missing")
            continue
        en = (TEXT / f"{doc}.html").read_text(encoding="utf-8")
        tr = path.read_text(encoding="utf-8")
        tag = f"{lang}/{doc}"

        a, b = skeleton(en), skeleton(tr)
        if a != b:
            for i, (x, y) in enumerate(zip(a, b)):
                if x != y:
                    problems.append(f"{tag}: markup diverges at tag no. {i + 1}: original {x}, translation {y}")
                    break
            else:
                problems.append(f"{tag}: tag count is {len(b)}, original has {len(a)}")

        have = Counter(PLACEHOLDER.findall(tr))
        want = Counter(PLACEHOLDER.findall(en))
        for key in ("version", "email"):
            if have[key] != want[key]:
                problems.append(f"{tag}: placeholder {{{{{key}}}}} occurs {have[key]} times, needs {want[key]}")
        for key in FORBIDDEN_IN_TRANSLATION:
            if have[key]:
                problems.append(f"{tag}: placeholder {{{{{key}}}}} is forbidden in the translation — write the phrase out directly, in the required form")
        extra = set(have) - {"version", "email"}
        if extra - set(FORBIDDEN_IN_TRANSLATION):
            problems.append(f"{tag}: extra placeholders {sorted(extra)}")

        text_en, text_tr = visible_text(en), visible_text(tr)
        if Counter(re.findall(r"\d+", PLACEHOLDER.sub("", text_en))) != Counter(re.findall(r"\d+", PLACEHOLDER.sub("", text_tr))):
            problems.append(f"{tag}: numbers are wrong (a term, amount or section number was lost)")

        words = re.findall(r"[A-Za-z']+", PLACEHOLDER.sub("", re.sub(r"<[^>]+>", " ", tr)))
        english = [w for w in words if w.lower() in ENGLISH_WORDS]
        if len(english) > 3:
            problems.append(f"{tag}: English leftovers ({len(english)}): {' '.join(english[:8])}")

        has_cyr = bool(re.search("[А-Яа-яІіЇїЄєҐґ]", text_tr))
        if lang in CYRILLIC and not has_cyr:
            problems.append(f"{tag}: no Cyrillic")
        if lang not in CYRILLIC and has_cyr:
            problems.append(f"{tag}: there is Cyrillic in the text")

        ratio = len(text_tr) / max(1, len(text_en))
        if not 0.6 <= ratio <= 1.9:
            problems.append(f"{tag}: length is {ratio:.2f} of the original — likely an omission or an addition")
    return problems


def main(argv: list[str]) -> int:
    langs = argv or [d.name for d in sorted(TEXT.iterdir()) if d.is_dir() and d.name in LANGS]
    if not langs:
        print("no translations at all")
        return 1
    bad = 0
    for lang in langs:
        problems = check(lang)
        print(f"{lang}: {'ok' if not problems else str(len(problems)) + ' issues'}")
        for p in problems:
            print("   -", p)
        bad += len(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
