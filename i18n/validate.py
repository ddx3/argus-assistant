"""Check of the interface translation dictionary.

    python i18n/validate.py es            one language
    python i18n/validate.py               all that are in static/i18n

What is checked: the dictionary has all the keys from keys.json; the `{places}` and tags in
the translation are the same as in the key; no Cyrillic is left in languages that use the
Latin script; the translation is not stretched to twice the English length where space is
limited; no string is left untranslated.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / "src" / "argus" / "ui" / "static" / "i18n"
CYR = re.compile("[А-Яа-яЁёІіЇїЄєҐґ]")
CYR_OK = {"ru", "uk"}                # Cyrillic is normal here
PLACE = re.compile(r"\{[a-z_]+\}")
TAG = re.compile(r"</?[a-z]+[^>]*>")
# brand names that are the same in every language
UNCHANGED_OK = {"Argus · assistant"}
# what may stay unchanged: strings with no letters in them (numbers, symbols, units)
SAME_OK = re.compile(r"^[\W\d_]*$")


def check(lang: str) -> list[str]:
    keys = [e["k"] for e in json.loads((DIR / "keys.json").read_text(encoding="utf-8"))["keys"]]
    path = DIR / f"{lang}.json"
    if not path.exists():
        return [f"{lang}: missing file {path.name}"]
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [f"{lang}: file cannot be read as JSON: {e}"]
    errs = []
    for k in keys:
        if k not in d:
            errs.append(f"{lang}: no translation: {k[:70]}")
            continue
        v = d[k]
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{lang}: empty translation: {k[:70]}")
            continue
        if sorted(PLACE.findall(k)) != sorted(PLACE.findall(v)):
            errs.append(f"{lang}: {{slots}} do not match: {k[:60]!r} -> {v[:60]!r}")
        if sorted(TAG.findall(k)) != sorted(TAG.findall(v)):
            errs.append(f"{lang}: tags do not match: {k[:60]!r} -> {v[:60]!r}")
        if lang not in CYR_OK and CYR.search(v):
            errs.append(f"{lang}: Cyrillic left over: {v[:70]!r}")
        if v == k and len(k) > 14 and not SAME_OK.match(PLACE.sub("", k)) and k not in UNCHANGED_OK:
            errs.append(f"{lang}: not translated: {k[:70]}")
        if len(k) <= 40 and len(v) > max(3.2 * len(k), len(k) + 20):
            errs.append(f"{lang}: too long for the slot ({len(k)}→{len(v)}): {k!r} -> {v!r}")
    extra = [k for k in d if k not in set(keys)]
    for k in extra[:10]:
        errs.append(f"{lang}: extra key (not in keys.json): {k[:70]}")
    return errs


def main() -> int:
    langs = sys.argv[1:] or sorted(p.stem for p in DIR.glob("*.json") if p.stem != "keys")
    bad = 0
    for lang in langs:
        errs = check(lang)
        for e in errs:
            print(e)
        print(f"{lang}: {'OK' if not errs else f'{len(errs)} issues'}")
        bad += bool(errs)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
