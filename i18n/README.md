# Interface languages

Nine languages: English (`en`, the source), Russian (`ru`), `uk`, Spanish (`es`), Brazilian Portuguese (`pt`),
German (`de`), French (`fr`), Turkish (`tr`), Polish (`pl`). The set follows where desktop conversation assistants
are used from: English as the source and the default for the rest of the world, the audience the project was first
written for (`ru`, `uk`), and the biggest Latin-script markets where an English interface is not enough (Spanish,
Portuguese, German, French), plus Turkish and Polish: many people in IT and remote work, and Polish also shares an
audience with `uk`. Arabic (right-to-left script), Chinese, Japanese and Korean are next in line: they need their own
layout and font checks.

## How it works

- The pages are written in English. **The English string in the code is the translation key.**
- English has no dictionary: the key already is the text, and with `en` the engine does nothing. Russian is not a
  source but one of the languages: `static/i18n/ru.json` is a dictionary like the other eight (`uk`, `es`, `pt`,
  `de`, `fr`, `tr`, `pl`), with English keys and Russian values.
- `ui/i18n.py` injects into every HTML page (as one layer over the whole app) the chosen language,
  the dictionary `static/i18n/<language>.json` (except for `en`) and the engine `static/i18n/engine.js`.
- The engine replaces ready-made text on the page: text nodes, hints (`title`,
  `placeholder`, `aria-label`, `alt`), the tab title. If there is no translation, the English
  string stays and nothing breaks.
- Conversation utterances, model answers and file names are not translated. The exception is
  the questions the app asks by itself ("Tell me what's happening on the screen."):
  they go to the model in the interface language.
- The language is set by `channel.assist.interface.language` (the settings page, "App language").
  "auto" means the Windows language, and if there is no translation for it, English. After the language changes,
  the pages reload by themselves.
- The answer and speech recognition languages are the same nine (one list: `assist/prompt.py`,
  `LANGUAGES`).

## A string with a placeholder and with tags

`{n}`, `{name}` in a key is a placeholder for a value: the key `{n} ms` fits both "120 ms" and "87 ms".
A key with a tag (`<b>…</b>`) is compared with the markup of the whole element, so a translation
can change the word order.

## If you edit the English interface text

The page does not break: the translation simply stops being found, and the string stays
English in every language. To bring the translations back:

1. Fix the string in `i18n/keys_manual.py` (for the settings page the strings are taken from
   `ui/admin.py` automatically). The place (`at`): `dash` the main window, `home` the session list, `record` "Work with
   the recording", `account` the account, payment and usage tabs, `settings` the settings window, `runtime`
   the refusal texts from Python and from the server. A string that the code assembles from pieces (`"… — wait " + n + " min"`,
   that is, "… — wait " plus a time period) is not present in the source as a whole, so add `[assembled in code from pieces]`
   to the note, otherwise the check in step 3 will consider the key dead.
2. `python i18n/gen_keys.py` rebuilds `static/i18n/keys.json`.
3. `python i18n/gen_keys.py --check` makes sure the strings are still in the code (text removed from the code
   leaves a dead key: delete it from `keys_manual.py` and from the dictionaries).
4. `python i18n/validate.py` shows what is missing in each dictionary.
5. Add the translation to `static/i18n/<language>.json`, `ru.json` included (the key is the English string, as in
   keys.json).
6. `python i18n/scan_pages.py` finds the English strings of the pages and of server refusals that have **no** key
   (the same way the engine will see them); `tests/test_i18n_coverage.py` does the same, and it is what fails the build.

A new string without an entry in `keys_manual.py` works, but stays English in all languages, and the test above does not
let it through. The scanner finds refusal texts from Python (`HTTPException`, `Denied`, `{"error": …}`) and the complaints in the failure window
(the functions `complaints`, `problems`, `hide_from_capture`…; the list is `RAISERS` and `MESSAGE_FUNCS` in
`i18n/scan_pages.py`) by itself; a message that reaches the person some other way has to be
added as a key by hand and, if it comes from a new place, that place has to be added to the scanner.
The engine looks for a key with a `{placeholder}` as `(.*?)`: the general "up to {n}" matches any text that starts that way, so
a more specific key must have more letters than the general one (the engine sorts by them), and the placeholder value
is translated by itself if it is an exact key ("too many sign-in attempts" inside "{what} — wait …").

## A new language

1. A line in `assist/prompt.py` (`LANGUAGES`): the code, the name in the language itself, the rule for the
   model. The lists of interface and recognition languages are built from it automatically.
2. The code in `ui/i18n.py` (`SUPPORTED`).
3. A dictionary `static/i18n/<code>.json` for all keys from `keys.json` (`i18n/GLOSSARY.md` has
   the terms and tone), checked with `python i18n/validate.py <code>`.
4. For the landing page: `landing/copy/<code>.json` and `landing/scenarios/<code>.json`
   (`landing/README.md`).

`tests/test_i18n.py` makes sure the three places (the list in the prompt, `SUPPORTED`, the dictionaries) do not
drift apart.

## What is not there yet

Translated: the main window, the session list and the dialogs above it, "Work with the recording", the settings window
(including "This session", "General" and the macOS hints), the "Account", "Payment" and
"Usage" tabs (balance, prices, history, password), and the messages that the app and the account
server show in the window. Still in English only: the console (`argus ...`), system dialogs (startup
error, update), server emails, and everything a person or the model wrote: utterances, answers,
file and session names. The terms and the policy are translated into all nine languages (`legal/text/<language>/`, `legal/README.md`). Not yet translated:
the assistant's answers to the recap commands ("итог" (recap) and "следи за" (watch for), typed in Russian; `recap/service.py`), which are ready-made text in the
answer timeline that the engine does not touch; the messages of the speech recognition client from the server
(`server/relay_stt.py`, being edited in other work), which have no keys yet; dates and numbers on the pages
are formatted by the browser according to the page language (`Intl`), not by a dictionary.
