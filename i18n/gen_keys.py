"""The canonical list of interface strings: the translation keys.

    python i18n/gen_keys.py           rebuild static/i18n/keys.json
    python i18n/gen_keys.py --check   check that the English strings are still in the code

An English string in the code is the translation key (see i18n/keys_manual.py). The texts of
the settings page are taken from `ui/admin.py` itself, so the list does not drift away from
what the person will see.
"""
import json
import re
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from keys_manual import MANUAL  # noqa: E402

OUT = ROOT / "src" / "argus" / "ui" / "static" / "i18n" / "keys.json"
SOURCES = [
    ROOT / "src" / "argus" / "ui" / "static" / n
    for n in ("index.html", "home.html", "record.html", "account.js", "chrome.js", "winbar.js",
              "update-button.js", "admin.js", "admin.html")
] + [p for p in (ROOT / "src" / "argus").rglob("*.py")]

# A note in the explanation: the string is assembled in code from pieces
# (`"… — wait " + period`, `f"{platform}"` for a platform name). The string as a whole is not in
# the sources, and the "is it in the code" check will not find it. The key is still live.
ASSEMBLED = "[assembled in code from pieces]"

# Language names are written in the language itself and are not translated.
ENDONYMS = {"\u0440\u0443\u0441\u0441\u043a\u0438\u0439", "українська", "English", "Español", "Português (Brasil)", "Deutsch",
            "Français", "Türkçe", "Polski"}


def admin_keys() -> list[tuple[str, str, str]]:
    from argus.assist import prompt as P  # noqa: F401
    from argus.tts.settings import TTSSettings
    from argus.ui import admin

    out: list[tuple[str, str, str]] = []
    # Some hints depend on the system ("Windows sometimes swaps the microphone by itself" /
    # "macOS …"): the strings are taken for both. Otherwise the list would depend on the machine
    # it was built on, and on another system `keys.json` would drift away from what the check
    # expects.
    for platform in ("win32", "darwin"):
        with mock.patch.object(sys, "platform", platform):
            sections = admin.sections(TTSSettings())
        for s in sections:
            out.append((s.title, "settings", "title of a settings section"))
            if s.lead:
                out.append((s.lead, "settings", "explanation under the section title"))
            for st in s.settings:
                if st.label:
                    out.append((st.label, "settings", "name of a setting"))
                if st.hint:
                    out.append((st.hint, "settings", "explanation of a setting"))
                if st.placeholder:
                    out.append((st.placeholder, "settings", "hint in an empty field"))
                for _value, label in (st.options or []):
                    if label and label not in ENDONYMS:
                        out.append((label, "settings", "item of a drop-down list; short"))
    return out


def collect() -> list[dict]:
    seen: dict[str, dict] = {}
    for key, at, note in list(MANUAL) + admin_keys():
        if key in seen:
            if at not in seen[key]["at"].split(","):
                seen[key]["at"] += "," + at
            continue
        seen[key] = {"k": key, "at": at, "n": note}
    return list(seen.values())


def fragments(key: str) -> list[str]:
    """Pieces of a key without `{value slots}` and tags: these are the pieces that must be present in
    the code.
    """
    plain = re.sub(r"<[^>]+>", "\0", re.sub(r"\{[a-z_]+\}", "\0", key))
    return [f.strip() for f in plain.split("\0") if re.search("[A-Za-z]", f)]


def check(keys: list[dict]) -> int:
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in SOURCES)
    text = text.replace("\\'", "'").replace('\\"', '"')      # an escaped quote inside a code string
    flat = re.sub(r"\s+", " ", text)
    # The settings page strings are taken from the running `ui/admin.py` (see `admin_keys`): they
    # cannot get lost, and searching for them in the source is not possible, because some are
    # assembled from pieces or depend on the system.
    auto = {k for k, _at, _n in admin_keys()} - {k for k, _at, _n in MANUAL}
    missing = []
    for e in keys:
        if e["k"] in auto or ASSEMBLED in e["n"]:
            continue
        for frag in fragments(e["k"]):
            norm = re.sub(r"\s+", " ", frag)
            # in the code long strings are cut into quoted pieces; we join them for the search
            if norm in flat or norm in re.sub(r"[\"'`]\s*\+?\s*[\"'`]", "", flat):
                continue
            missing.append((e["k"][:80], frag[:60]))
    for k, frag in missing:
        print("missing from the code:", frag, "  <-", k)
    print(f"keys: {len(keys)}, not found in the code: {len(missing)}")
    return 1 if missing else 0


def main() -> int:
    keys = collect()
    if "--check" in sys.argv:
        return check(keys)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"keys": keys}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("written", OUT, len(keys))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
