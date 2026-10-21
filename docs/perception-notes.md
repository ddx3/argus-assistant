# Argus: notes on the perception core

> Early engineering notes. They were written when speech recognition ran on a local
> Whisper; the engines are now swappable, and the main one runs in the cloud through the
> account server. The measurements below date from that time, while the conclusions (about
> jargon, the slide into dialogue, reading the window) still hold. For the current overview
> see the [README](../README.md) and [architecture.md](architecture.md).

A desktop assistant for conversation: screen + system audio + microphone → model.

It hears you and the other party on **separate channels**, sees the screen, recognizes speech
and text, merges everything into one timeline, and the model suggests an answer from it. Below
is the perception core itself.

## Running

```
uv sync
uv run argus doctor    # check hardware, audio, screen, models
uv run argus run       # the app window
```

The interface opens in **its own window**, not in a browser tab. Inside it is the same
local server, shown through WebView2 (it ships with Windows) or WKWebView on macOS. There is no
second implementation of the interface and there will not be one: we would have to write it twice
just for the address bar, which shows up when the screen is shared.

**macOS.** It runs on Windows and macOS 14+ (Apple Silicon). The macOS code lives in
`src/argus/mac/`, and the build is `bash packaging/build_macos.sh`. It was written without a Mac, but
it builds and runs on a real macOS (build and smoke test in GitHub Actions, the DMG
is published for download); no one with a Mac has tried the app yet. Status, risks and the order
of the first check are in [`docs/macos.md`](macos.md). On a Mac, before `uv run argus run`, build the
system audio helper: `swift build -c release --package-path macos/helper`.

The browser is still there for debugging, and it has the developer
tools:

```
uv run argus run --browser
```

What opens is the **list of sessions**, not the working screen. A session is one conversation:
it has its own settings (context, tone, language, when to answer), its own materials and
its own answer history, which comes back when the session is opened again. The working
screen is inside a session (`/chat/<id>`); the "←" in its header leads back to the list.
Sound, hotkeys, voice output and the account are shared by all sessions. The data lives in `chats/`.
The title can be skipped; it is invented automatically from the description of the conversation.

The dashboard comes up at once, but **recognition waits for a button**: first you choose the
window and the sources, then you turn it on. Otherwise random things leak into the timeline,
and they cannot be removed from it afterwards. The `--start` flag turns it on right away.

In the dashboard: the recognition switch, checkboxes per source (microphone,
system audio, screen), **the choice of the window to read** and, most important, **what exactly went
to the model**: the full text of the request next to the answer.

**What the model sees.** The whole screen, as a picture. Not text extracted from the screen,
but the picture itself: the model reads it the way a person does. A check on a live screen:

> There is a Twitch stream on the screen right now: the streamer is playing Overwatch. The frame shows
> a moment of combat with an explosion effect and the caption "TRAPPED!", the opponent's health
> is 79%. On the right is the stream chat.

Text recognition runs alongside and also goes into the timeline, but it is a supplement:
it gives memory of what was on the screen earlier, because the picture goes to the
model only at the moment of a request, while the text is written all the time and almost for free.

One checkbox, "Screen", turns all of this on.

Either the whole screen or a pinned window is read; you choose in the dashboard from
thumbnails taken of each window separately, so you can see what you are choosing.
There is deliberately no following of the active window: it would mean reading
something random — switch to the mail, and we read the mail.

The pinned window is captured **on its own**, through Windows Graphics Capture,
and is not cut out of a screenshot. So it is read even if it lies under
another window or is moved to another desktop: you can keep a stream or a
call off to the side and work in your own window. Neither cropping from the screen nor PrintWindow
can do this: the first cannot see what is not on the screen, the second does not capture
video (measured on Chrome with a stream: in the center of the window 0% non-empty pixels
against 99% for Graphics Capture). "The model talks nonsense" and "nonsense went into the prompt" are different
breakages, and you can tell them apart only by seeing the input.

## Two recognition engines side by side

Jargon is the weak spot of recognition: "Roshan" becomes "Roshen", "creeps" becomes
"clips". A hint with words props this up, but a prop is only a prop: the
terms must be known in advance and maintained by hand, and new slang it does not catch
by definition. The ceiling here is structural: a transcriber has to choose a spelling
relying only on the language prior.

An audio-native model reads the same sound with the accumulated conversation in front of it:
"Roshan" is recognized not from a list but because the last five minutes
were about Dota. Whether this works on live speech is a question for measurement.

So the second engine is turned on **next to the first**, on the same sound:

```toml
[stt]
compare_with = "realtime-openai"
```

The key goes into `.env` next to the config; the file is in `.gitignore` and is picked up
automatically:

```
OPENAI_API_KEY=sk-...
```

An environment variable, if it is set, wins over the file: a temporary key in a
terminal must override the stored one, not the other way around.

Our VAD cuts the speech into utterances, and both engines get **the same**
segment. Otherwise the differences would come from phrase boundaries, not from recognition.

In the dashboard the "Conversation" panel splits into two columns with the differences highlighted,
and in the replay there is a line-by-line comparison with a summary:

```
   3.2s  THEM
  whisper-local      they push mid, Roshen in a minute      1180 ms
  realtime-openai    they push mid, Roshan in a minute       620 ms
  ↑ differ

matched 1 of 5, differed 3
latency: whisper-local 1040 ms, realtime-openai 602 ms
```

A difference in punctuation only does not count as a mismatch; otherwise real
jargon errors would drown in formatting noise.

**The second engine changes nothing while the first one works.** Its text goes neither into
the timeline, nor into the prompt, nor into the filter's decisions, and it adds no latency to the main
path: it lives in its own thread. If it drops out (no key, the network is down,
the limit is used up), recognition keeps working on the main engine. Otherwise turning on the
measurement would change what is being measured.

The only exception is when the main engine produced nothing. Then the second engine's
utterance is substituted: it listened to exactly the same segment, and at that moment there is
nothing left to wait for, because the main path has failed. In the timeline such an utterance is ordinary, but it is marked
`rescued_by`, because frequent substitution means the wrong engine was chosen as the main one. This
is what makes the pair meaningful when the main engine works over the network: connection,
limit and the slide into dialogue are three failures that a local engine does not have.

### The slide into dialogue

Measured on the live API: a conversational model used as a transcriber occasionally
**answers a question it heard instead of writing it down**. And the failure
sustains itself: the answer lands in the session history, and from then on the model carries on
a dialogue instead of transcribing.

Two things in the code follow from this:

- **The model's own transcriptions are not dropped from the history.** Verified by
  measurement: if they are removed, the share of failures grows from 1/15 to 9/15. The model stays in
  its role by looking at its own previous answers, which work as an example.
- **Text that is too fast is discarded.** Speech runs at 1.3–2.0 words per second,
  answers at 2.75–9.25; the `max_words_per_second` threshold sits in between. A discarded
  utterance shows up in the comparison as "not recognized", while a passed-through answer would silently
  pass itself off as a disagreement between engines, and the measurement would lie.

It is most often caught on speech addressed directly to the listener ("tell us
about…"). On gaming chatter that is addressed to no one, there are noticeably fewer failures.

## Commands

```
argus doctor                 check the environment
argus devices                audio devices with indexes
argus capture                live sound levels and frames
argus listen                 speech and screen recognition in the terminal
argus transcribe f.wav       recognize a ready WAV, without a microphone
argus run                    the whole pipeline with the dashboard and the model
argus run -s 600             the same, but stops by itself after 10 minutes
argus record --seconds 60    record a session to disk
argus record --no-screen     record only sound, without frames
argus sessions               list of recordings
argus replay 1153 --stt --ocr   run a recording through the pipeline
argus replay 1153 --stt         compare two engines, if compare_with is on
argus account-server --dev   account server for development (stub cashier, mail to the console)
argus sync-voices            create characters for all voices available to the ElevenLabs account (free)
argus browse-voices -l ru    browse the shared voice library, adding nothing
argus import-voice <owner> <id> <name>   add a voice from the shared library to the account and create a character
argus add-voice <id> <name>  create a character for an already available voice_id, without cloning
argus clone-voice f.wav <name>   clone a voice from a 30–60 second sample (experimental)
```

## Structure

```
core/         clock, events, bus, config
capture/      WASAPI (microphone + system audio) and DXGI (screen)
perception/   speech (Silero VAD + Whisper) and screen (frame filtering + RapidOCR)
context/      the single event timeline, from which the prompt is built
brain/        when to wake the model and how to address it
session/      session recording and replay
chats/        sessions: a folder on disk, the open session, answer history, titles
ui/           the home page with the session list, the dashboard, the settings page
```

The microphone and the system audio go in **separate streams**: the microphone is "me",
the system audio is "they are speaking to me". This replaces speaker recognition for free.

## Measurements on an RTX 3070 / Ryzen 9 5900X

| stage | result |
|---|---|
| Speech detection (Silero VAD) | x227 real time, on the CPU |
| Whisper large-v3-turbo | x8–17, ~1.3 GB of video memory out of 8 |
| From the last word to text | ~1.0–1.15 s |
| Screen reading (RapidOCR) | ~1.2 s for a dense screen, downscaled to 1280 px |
| Frame filtering | 2–5% of frames reach recognition |
| Model answer (through the subscription) | 1.6–5.2 s, large spread |
| Screen picture in the request | +220 ms, ~27 KB, ~1000–1500 tokens |
| Recording replay | x5–21 real time |

The end-to-end latency "the other party's question → the answer on screen" is about 3–4 seconds,
more than half of which is the model.

## Development

```
uv run pytest        # the whole suite; we don't state the number of tests here, it goes stale within a day
uv run ruff check .
```

Python 3.12 (there are no CTranslate2 wheels for 3.13+). There is deliberately no PyTorch in the project:
recognition is built on ONNX Runtime and CTranslate2, which is ~2 GB instead of ~10.

In the shipped `argus.toml` the model goes through our account server (`brain.provider = "relay"`:
the key stays on the server, the address is `[account] server`), while in the code itself the default is `stub`
(canned answers, no network and no money). A Claude subscription (`brain.provider = "claude-code"`) is
a working loop for **development**: it does not cover distributing the product to other people, and for that
the config has `relay`, `anthropic-api` and `openai-api`.
