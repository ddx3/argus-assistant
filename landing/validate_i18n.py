"""Checks the landing translations: the page texts (copy/) and the sample conversation
(scenarios/).

    python validate_i18n.py es        one language
    python validate_i18n.py           all that have files

copy/<lang>.json: the same ids as in copy/en.json (the source); the same tags and {slots}.
scenarios/<lang>.json: the same shape as scenarios/en.json; the code block and the formulas
are kept; in languages with the Latin alphabet there is no Cyrillic.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
CYR = re.compile("[А-Яа-яЁёІіЇїЄєҐґ]")
CYR_LANGS = {"ru", "uk"}
TAG = re.compile(r"</?[a-z]+[^>]*>")
SLOT = re.compile(r"\{[a-z_]+\}")
CODE_BLOCK = re.compile(r"```python\n.*?\n```", re.DOTALL)
FORMULA = re.compile(r"\$\$.*?\$\$", re.DOTALL)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def check_copy(lang: str) -> list[str]:
    src = {k: v for k, v in load(HERE / "copy" / "en.json").items() if not k.startswith("_")}
    path = HERE / "copy" / f"{lang}.json"
    if not path.exists():
        return [f"{lang}: copy/{lang}.json is missing"]
    d = load(path)
    errs = []
    for k, en in src.items():
        if k not in d:
            errs.append(f"{lang}: missing string {k}")
            continue
        v = d[k]
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{lang}: empty {k}")
            continue
        if sorted(TAG.findall(en)) != sorted(TAG.findall(v)):
            errs.append(f"{lang}: tags {k}: {TAG.findall(en)} -> {TAG.findall(v)}")
        if sorted(SLOT.findall(en)) != sorted(SLOT.findall(v)):
            errs.append(f"{lang}: {{slots}} {k}")
        if lang not in CYR_LANGS and CYR.search(v):
            errs.append(f"{lang}: Cyrillic in {k}: {v[:60]!r}")
    for k in d:
        if k not in src and not k.startswith("_"):
            errs.append(f"{lang}: extra id {k}")
    return errs


def shape(x):
    if isinstance(x, dict):
        return {k: shape(v) for k, v in x.items() if not k.startswith("_")}
    if isinstance(x, list):
        return [shape(v) for v in x]
    return None


def check_scenario(lang: str) -> list[str]:
    ref = load(HERE / "scenarios" / "en.json")
    path = HERE / "scenarios" / f"{lang}.json"
    if not path.exists():
        return [f"{lang}: scenarios/{lang}.json is missing"]
    d = load(path)
    errs = []
    if shape(d) != shape(ref):
        errs.append(f"{lang}: shape does not match en.json (fields, number of turns, table rows)")
        return errs
    for i, (a, b) in enumerate(zip(ref["answers"], d["answers"])):
        if CODE_BLOCK.findall(a) != CODE_BLOCK.findall(b):
            errs.append(f"{lang}: answer {i + 1}: the code block must stay as is")
        if len(FORMULA.findall(a)) != len(FORMULA.findall(b)):
            errs.append(f"{lang}: answer {i + 1}: the number of $$…$$ formulas differs")
        if "\\approx" in a and "\\approx" not in b:
            errs.append(f"{lang}: answer {i + 1}: \\approx was lost")
        if a.count("`") != b.count("`"):
            errs.append(f"{lang}: answer {i + 1}: different number of backticks")
    if len(d["call"]["names"]) != 4 or len(d["call"]["initials"]) != 4:
        errs.append(f"{lang}: there must be four people on the call")
    for ini in d["call"]["initials"]:
        if len(ini) > 3:
            errs.append(f"{lang}: initials are too long: {ini}")
    if lang not in CYR_LANGS:
        flat = json.dumps(d, ensure_ascii=False)
        m = CYR.search(flat)
        if m:
            errs.append(f"{lang}: Cyrillic in scenario: …{flat[max(0, m.start() - 30):m.start() + 30]}…")
    return errs


def main() -> int:
    langs = sys.argv[1:] or sorted(
        {p.stem for p in (HERE / "copy").glob("*.json")} | {p.stem for p in (HERE / "scenarios").glob("*.json")}
    )
    bad = 0
    for lang in langs:
        errs = []
        if lang != "en":
            errs += check_copy(lang)
        if lang != "en":
            errs += check_scenario(lang)
        for e in errs:
            print(e)
        print(f"{lang}: {'OK' if not errs else f'{len(errs)} issues'}")
        bad += bool(errs)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
