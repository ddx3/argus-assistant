"""Builds the terms and privacy policy pages, in all the app's languages.

    python legal/build.py              -> the download page + embedded in the app
    python legal/build.py --release    the same, but fails if operator.json is not filled in
                                       or a translation is missing

The original is English (`legal/text/<document>.html`); the translations into the other eight
app languages are in `legal/text/<lang>/<document>.html` (requirement: localize the terms and
conditions into all languages; before that the documents were English only). A translation is
for ease of reading: each one says that in case of a discrepancy the English version prevails.
The structure of the translations is guarded by `legal/validate_translation.py` and tests;
only a human can check the meaning — the project has no lawyer, and this must be known
(`legal/README.md`).

Each page carries all the languages at once (`legal/legal_render.py`): English is visible
without scripts, and the others are turned on by the page script — in the app by the
interface language, on the site by the person's choice and the browser language.

The same page is put in two places:

- `deploy/server/downloads/` — next to the download page; the landing page and the download
  page link to it (in the browser the document opens as an ordinary page);
- `src/argus/ui/static/legal-<name>.html` — inside the app. There the document is shown in a
  window over the page (`/p/legal-<name>` is the shared route for pages, documents have none
  of their own), it does not take the person to the browser and works without a network. It
  is also exactly the text under which the person pressed "Create account":
  the terms version (`account/legal.py`) is raised together with it.

The places for what we cannot know ourselves — who the operator is, whose law applies, where
to write — stand in the English text as `{{name}}`, `{{email}}`, `{{law}}` and are taken from
`operator.json` (in the translations the operator and the law are written directly, in the
needed form; `{{email}}` and `{{version}}` are substituted there too). While a field is
empty, the document keeps a yellow marker: the page can be seen and read, but it must not be
published. `--release` checks this: a gap in a document that goes out to people means terms
without an address to write to. We never write the operator's personal name or country in the
documents at all (a deliberate decision).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))

import legal_render as render  # noqa: E402  (shared part: languages, dates, articles)
from argus.account.legal import DOCS, TERMS_VERSION  # noqa: E402

# Where the finished page goes and under what name: the site — `terms.html`, the app —
# `legal-terms.html` (the shared route `/p/<name>` serves files only from one flat folder).
OUTPUTS = (
    (ROOT / "deploy" / "server" / "downloads", ""),
    (ROOT / "src" / "argus" / "ui" / "static", "legal-"),
)

TITLE = {"terms": "Terms of Use", "privacy": "Privacy Policy"}

# Legacy: tests and scripts take it from here.
HINT = render.HINT
human_date = render.human_date
SLOT = render.SLOT

# Languages without a translation after the latest build (for `run`).
ABSENT: dict[str, list[str]] = {}


def build(outputs: tuple[tuple[Path, str], ...], operator: dict,
          version: str = TERMS_VERSION) -> set[str]:
    """Build the pages: pairs (folder, file name prefix).

    Returns the names of the unfilled operator fields."""
    template = (HERE / "template.html").read_text(encoding="utf-8")
    missing: set[str] = set()
    ABSENT.clear()
    langbar = "".join(f'<button type="button" data-lang="{lang}">{render.UI[lang]["name"]}</button>'
                      for lang in render.LANGS)
    # A space between the closing braces: substitutions in the texts are searched for by `}}`
    # (tests and `release_guard.legal_problems` treat such a pair as an unfilled field), and the
    # nested dictionary ends with exactly that.
    ui_json = json.dumps(render.UI, ensure_ascii=False).replace("}}", "} }")
    langs_json = json.dumps(list(render.LANGS))

    for doc in DOCS:
        other = "privacy" if doc == "terms" else "terms"

        def footer(lang: str, _doc: str, other: str = other) -> str:
            ui = render.UI[lang]
            return (f'<footer>\n    <a href="{other}.html">{ui[other]}</a>\n'
                    f'    <a href="./">{ui["download"]}</a>\n  </footer>\n')

        body, absent = render.articles(doc, operator, version, missing, footer)
        if absent:
            ABSENT[doc] = absent
        page = (template.replace("{{ARTICLES}}", body)
                        .replace("{{LANGBAR}}", langbar)
                        .replace("{{UI_JSON}}", ui_json)
                        .replace("{{LANGS_JSON}}", langs_json)
                        .replace("{{DOC}}", doc)
                        .replace("{{TITLE}}", TITLE[doc]))
        for out, prefix in outputs:
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{prefix}{doc}.html").write_text(page, encoding="utf-8")
    return missing


def run(operator: dict, outputs: tuple[tuple[Path, str], ...], release: bool = False) -> int:
    """Build and say what is missing. The return code is for `--release`."""
    missing = build(outputs, operator)
    for out, prefix in outputs:
        print(f"built: {out / (prefix + '*.html')} (version {TERMS_VERSION}, languages: {len(render.LANGS)})")
    bad = 0
    if missing:
        print(f"NOT FILLED IN in legal/operator.json: {', '.join(sorted(missing))}")
        bad = 1
    if ABSENT:
        for doc, langs in sorted(ABSENT.items()):
            print(f"NO TRANSLATION: {doc} - {', '.join(langs)}")
        bad = 1
    if bad and release:
        print("must not be published: fill in the fields, finish the translations and rebuild")
        return 1
    return 0


def main(argv: list[str]) -> int:
    operator = json.loads((HERE / "operator.json").read_text(encoding="utf-8"))
    return run(operator, OUTPUTS, release="--release" in argv)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
