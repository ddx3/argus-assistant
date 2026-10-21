# Landing page

A page with the real Argus interface in nine languages. **Since 2026-10-13 it lives on our own server**
(`http://203.0.113.10/`) instead of a hosted artifact: `python deploy/landing/publish_landing.py` publishes it. How
everything is set up and what the "Download" button (`/download`) does is described in `deploy/landing/README.md`. We no longer
update the artifact. The app window is taken from `src/argus/ui/static/` as is (index.html, chrome.css, chrome.js, account.js, temml) and
is embedded in the page together with the translation engine and the app's own dictionaries
(`i18n/README.md`). In place of the server it gets a conversation script (`stub.js`, timings
in `build_demo.py`), so the app is not started for the build and nothing costs money.

The window is shown as it looks on a PC (the app will be desktop-only): the page header moved into the
top strip with the window buttons. The real `winbar.js` draws it, embedded in the demo with a patch (`demo_winbar` in
`build_demo.py`: the "not in an iframe" check is removed and a main-window marker is set); the answers window, which sits over the call,
has no strip, same as in the app. The demo address `/chat/demo` is needed so the page can show the "Work with the recording" arrow.

The sample conversation, the screens, and the answers are made up, and each language has its own (`scenarios/`). The interface labels ("QUESTION", "Microphone",
"RECOGNITION IN PROGRESS") are the real ones and are also in the language of the page.

## Build

    python shots.py            # frames of the working window for all languages -> img/<lang>/
    python screens.py en ru    # frames of "Sessions" and "Record" (the real home.html and record.html) -> img/<lang>/
    python voice_shots.py en   # frames of the voice replies (the real voice picker from the settings) -> img/<lang>/
    python build_landing.py    # the page -> out/landing.html, the images -> out/img/
    python ../deploy/landing/publish_landing.py   # publish to the server (it builds into a temporary folder itself and does not touch `out/`)

Needs playwright (with Chromium) and Pillow. The system Python does not have them: they live in an environment outside the repository,
`.landing-venv`; set `PYTHONIOENCODING=utf-8` before running. The page is read from `out/landing.html`, and the images of the
selected language are loaded from `img/<lang>/` next to it, so you must publish both the page and the `img/` folder.

## What is where

- `template.html` — the page itself: English texts with `data-i18n` markers, styles
- `page.js` — language (English by default, the choice is remembered; the language picker sits in the header in place of the download button, its markup in `lang_picker.html`
  is embedded in the template), text substitution, launching the live window
- `copy/<lang>.json` — the page texts; `copy/en.json` is extracted from `template.html` (and `price_copy.py`) during the
  build (the template is the source of truth); the other languages, Russian included, are translations edited by hand
- `scenarios/<lang>.json` — the sample conversation: lines, three answers, texts of the fake screens,
  call participants. The reference for the shape is `scenarios/en.json`
- `build_demo.py` — scenario timings, the fake screens, assembling the app window into a single HTML file
- `stub.js` — a fake websocket and `fetch`; plays the scenario in a loop
- `shots.py` — shoots the poster and the "how it works" frames from frozen demo states
- `voice_shots.py` — the voice replies frames (`voice_idle`, `voice_play`): the real settings page (`/admin`) with the
  `voice-picker` widget and made-up voices. It takes the schema from `out/_schema.json` (made by `dump_schema.py`)
- `screens.py` — the "Sessions" and "Record" frames: the real app pages, with made-up data
  from `scenarios/screens/<lang>.json` in place of the server; the labels that are not yet in the app dictionary (the
  "Sessions" and "Record" pages are not translated) are in `scenarios/screens/<lang>.ui.json`, for the frames only

## "How it works": five cards

The `#how` section: five cards at once (system audio, microphone, screen, answers, voice) and under them the "Remembers" row (sessions and recording).
The cards have no boxes (requirement, 2026-10-12: no blocks inside blocks): no card border and no dark box around the window; on top there is a title and one line, under them the window itself, and frames without an edge of their own get a thin outline. The microphone chip and the feed row are a single rounded piece.
Each card has a small window made of frames of the real interface and its own short animation loop (`CARD_DEFS` in `page.js`: what happens and when,
in ms). The cards are independent of each other and play only while visible (one `IntersectionObserver` per card); off screen they
freeze, and when they come back they start from the beginning. Decision of 2026-10-08: the diagram of six windows ("variant D") turned out to be too confusing, and a tour
in a single window with tabs ("variant A") did not work either (separate tabs for the microphone make no sense), so variant C was chosen. Diagram D and the
tour are in `archive/variant-D` and `archive/variant-A-tour`.

- The frames are shot by `shots.py` and `voice_shots.py`: the real window picker with a made-up list (`pick_0..2`; the list is `windows` in
  `scenarios/<lang>.json`, the thumbnails are drawn by `build_demo.fake_windows`), the microphone chip with the level line at different heights
  (`chip_mic_0..5`, which switch quickly so the line "breathes"), a feed row (`row_me`), the screen panel (`see_idle`, `see_code`, `see_math`),
  the "thinking" card and the answer cards in a narrow window (`card_think`, `card_text`, `card_code`, `final_math`), the voice picker from the
  settings (`voice_idle`, `voice_play`). Only these belong to the page itself: the mouse in the picker window, the reading bar on the screen, and the wave on the voice card.
- Layout: three cards in the first row and two wide ones in the second; two columns below 980px, one column below 640px. The animation is the same at any width:
  there are no iframe windows in the cards, only frames.
- Without motion (`prefers-reduced-motion`): every card shows its final state at once.
- The class of the block with the microphone chip is `micchip`, not `chip`: `.chip` on the page is already taken by the language chips.
- For testing, you can send the header window `{argus: "demo", cmd: "seek", t: <second>}` (and `pause`/`play`).
- Other languages: the frames for them have not been shot (`python shots.py <lang>` and `python voice_shots.py <lang>`); the window names in the picker list
  come from the English scenario until the scenario of the language has `windows`.

### "Remembers": sessions and recording

The `#mem` block sits under the five cards. History: the block was first redone completely (it had been text with three points and tabs), and then the version with two cards and an arrow was judged to have a decent idea but a weak implementation. Now it has no cards and no boxes: two
real windows of 640px, overlapping (a stage of 1160x640, the windows are given in fractions of its width and shrink together with it at other widths). The loop (`memRun` in
`page.js`, ~18 s): both windows are on the stage from the very beginning (requirement, 2026-10-09: two windows at once, and the active one simply comes to the front), the sessions window is in front, the recording window is behind and dimmed; the mouse finds the first row (its buttons become visible), presses the recording button, the recording window comes to the front (the `opened` class on `#mem` changes the layer order and the dimming), the sessions window stays behind, the request is typed and sent ("Reading the recording…"), the finished answer fades in line by line, the mouse clicks the second
time in the answer (10:32), the conversation text opens, and the list scrolls to the right line. Off screen it freezes; without motion
(`prefers-reduced-motion`) it shows the final result at once. At the end the mouse moves into the sessions window and clicks a row: the sessions window is in front again and the loop starts over.

The finale is live (requirement, 2026-10-09: make a nicer animation, like the one when a window opens in the app, and carry it over to the landing page). The animation of the window
with text is done by the recording page itself (`record.html`: a curtain drops from the header, rows slide in one by one, answers dim, the list scrolls smoothly to the line, and a highlight flashes on it), so in the finale the real recording page is laid over the frame `mem_r_3` (`.mlive`, an iframe
of 640px, scaled to the window width) and the mouse clicks the real time in the answer. How it is built: `build_demo.build_record()` embeds `record.html` the same way
as the dashboard demo (styles, scripts, the `demo_winbar` window strip), the server is replaced for it by `stub_record.js` (the recording and the sessions come from `screens.Stand`,
the same as on the frames), and the language and data are set by `page.js` at launch (`RECDEMO`, `RECDATA`). The page reports `rec-ready` and where the
time in the answer is (`rec-rect`): the viewer has their own fonts, so the mouse moves to the real position and not to the position in the frame. Commands for it: `rect`, `cite`, `reset`.
If it did not come up (no `rec-ready`) or there is no motion, the finale is as before: the finished frame `mem_r_4`.

- The frames are shot by `screens.py` (the real `home.html` and `record.html`): sessions `mem_s_idle`, `mem_s_hover`, `mem_s_press` (the first five
  rows), recording `mem_r_0` (empty, the app's hint), `mem_r_2` ("Reading the recording…"), `mem_r_3` (the answer), `mem_r_4` (after the click on the
  time), and six slices of the input line with the text typed up to the fifth, ninth... letter, `mem_t_1..6`: they lie on top of `mem_r_0`, so the request
  appears to be typed. The recording labels that are not in the app dictionary are in `scenarios/screens/<lang>.ui.json`. The mouse clicks the second time in the answer
  (`MEM_CITE = 1` in `screens.py`, `stub_record.js`): its line (10:32:20) is not in view in the recording, so the list scrolls to it, and the made-up recording in
  `scenarios/screens/<lang>.json` starts with six lines of small talk before the conversation begins, for the sake of this scroll.
- Where the recording button, the input line, and the time in the answer are in the frames, `screens.py` writes to `img/<lang>/mem.json` (fractions of the frame); `build_landing.py`
  inserts this into the page (`{{MEMPOS}}` in `page.js`). Redraw the frames and rebuild, and the mouse and the slices land in the right places on their own.
- Traps: for slices and chips that switch faster than the smooth fade-in runs, the own transition of `.swap>img.on` wins; they need
  `transition:none` with the same specificity; `display:block` on a block with the class `swap` breaks the stack (the frames line up one under another).
- Narrow screen (below 900px): the windows stack one under another, and the inactive one is dimmed.

## Checks

    python validate_i18n.py                 # texts and scenarios of all languages
    python check.py en de                   # the page in Chromium: errors, scrolling, screenshots
    ..\.venv\Scripts\python dump_schema.py  # settings schema from the real panel
    python check_settings.py de tr          # the settings page: what still shows Cyrillic text

## A new language on the landing page

`copy/<code>.json` (all ids from `copy/en.json`), `scenarios/<code>.json` (the shape like
`scenarios/en.json`), the language code in `LANGS` (`build_demo.py`, `page.js`), and an entry in the header language list
(`#lang-menu` of the template), then `shots.py <code>` and `build_landing.py`. The app's own dictionary
for this language is needed too (`i18n/README.md`).

If the app interface changed, rebuild the frames (`shots.py`) and the page: it shows
what lies in the sources at the moment of the build.

## Links to the terms and the policy

In the template and in `copy/<lang>.json` they sit between the markers `<!--legal-->…<!--/legal-->` (the caption under the
button, the "Legal" column in the footer, a sentence in the answer about data). The `LEGAL` flag in
`build_landing.py`: `False` means the page is produced without them. While the documents are not published (the download page returns 404) and `legal/operator.json` is empty, keep `False`; when `terms.html` and `privacy.html`
open at the addresses from the template, set `True` and rebuild.

## What is enabled

Right now the page has only English (`ENABLED`). The texts of the other languages in `copy/` lag behind
English: they lack the new strings about sessions, recording, balance, and the five other cards, and
the page would take them from English. Before enabling a language, add the missing strings
(`python validate_i18n.py <lang>` shows which ones) and shoot `screens.py` for it.

## Separate pages: pricing, terms, policy (2026-10-12)

Besides `out/landing.html`, `build_landing.py` builds `out/pricing.html`, `out/terms.html`, `out/privacy.html` (the `subpages.py` module) and a copy of the home page `out/index.html`
(on the published site the home page is called `index.html`, and the pages link to one another by file name). The header, the footer, and the styles are taken from the home page template
(the markup after the disabled places are cleaned out), and the language is chosen with the same key `argus.landing.lang`, so the choice carries over between pages.

- **Pricing.** The source of the texts is `price_copy.py` (`ROWS`: key, Russian, English; the English column is the source, the Russian one is a translation), the other languages are in `copy/<lang>.json` (keys `price.*`, `nav.price`).
  The numbers in the table come from `src/argus/server/pricing.py` (`price_sheet()`: $0.60/h, $0.96/h smart, ≈$0.002 and ≈$0.019 per answer, $0.24 per 1000 characters; "what $5 gives" is calculated from the same place);
  if the price list changes, fix `subpages.pricing_body()` and `price.five.*`. The currency is formatted by the browser according to the language (`data-usd`). There are no models or providers on the page.
- **Terms and policy.** The texts are `legal/text/*.html` in English, the operator fields come from `legal/operator.json`; while they are empty, the pages show yellow notes (the build prints what is missing).
- **Publishing.** All pages are published by `deploy/landing/publish_landing.py` as a single release (before 2026-10-13 they went to the artifact through `files`).
- **Links to the documents** on the home page: the "Legal" column in the footer and a phrase in the answer about data (`LEGAL = True`); the line "By downloading Argus, you accept…" under the buttons stays off (`FINE = False`).

## A three-page header and the help page (2026-10-13)

The anchor tabs are gone: the header has "About" (the home page, `index.html`), "Pricing" (`pricing.html`), and "Help" (`help.html`). The sections of the home page ("How it works", "Remembers", "Invisibility", "Features")
remain as text on the home page, and the footer has links to them. The questions and answers moved from the home page to the help page: in the template the section is wrapped in `<!--faq-->…<!--/faq-->`, `build_landing.py`
cuts it out of the home page, and `subpages.help_body()` takes it from the same place (the contact details and the refund information are there too; the operator's email comes from `legal/operator.json`, and if it is empty, a yellow note appears).
The texts of the tabs and of the page are in `price_copy.py` (`nav.about`, `nav.help`, `help.*`, `faq.tag`), the other languages in `copy/<lang>.json`. The tab highlighting on scroll from `page.js` is removed.
Five files are published: the home page plus `pricing.html`, `help.html`, `terms.html`, `privacy.html`.

## The "Download" button and explanation pages (2026-10-13)

There is no separate download page. All buttons ("Download" on the first screen, in the finale, and on the pricing page) lead to `/download`, and the footer links lead to
`/download/windows` and `/download/mac`; they open in the same tab (they must not have `target="_blank"` or the `download` attribute, and the publishing step checks this).
Which file to serve is decided by nginx from the browser string (`deploy/landing/nginx/default.conf`). If there is no file for the system (right now that is Windows) or it is a phone,
the server redirects to `/?dl=soon` or `/?dl=other`, and `page.js` shows a small explanation at the bottom (`#dl-note`, keys `dl.soon`, `dl.other`) and removes
`?dl=…` from the address. What to do on the first launch on a Mac without an Apple signature is the question `faq.13` on the help page.
