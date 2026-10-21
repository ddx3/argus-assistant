"""Argus command line."""

from __future__ import annotations

from argus.console import setup_stdio

setup_stdio()  # before any output: otherwise the Windows console crashes on Cyrillic

import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer
from rich.table import Table

from argus.capture.audio import DeviceNotFound
from argus.console import console
from argus.core.config import Config, find_config

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Argus — perception core: screen + system audio + microphone.",
)

OK = "[green]ok[/green]"
FAIL = "[red]no[/red]"
WARN = "[yellow]?[/yellow]"


def _check_python() -> tuple[str, str]:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) != (3, 12):
        return WARN, f"{ver} — the project targets 3.12"
    return OK, ver


def _run_check(check, *args) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """One doctor check. Its own failure is also a result, not an abort.

    The doctor is needed exactly where something is broken: a library does not import, a program
    does not answer. An exception from one check used to carry away the whole table — the other
    eight rows never appeared.
    """
    from rich.markup import escape

    try:
        return check(*args)
    except Exception as e:  # noqa: BLE001 — the doctor must not crash itself
        return FAIL, escape(f"the check failed to run: {type(e).__name__}: {e}")


def _check_settings(cfg_path: Path | None) -> tuple[str, str]:
    """Whether the settings contain keys the program does not have.

    The models silently skip extras, and a typo (`cooldown` instead of `cooldown_s`) looks like a
    setting that does nothing.
    """
    from rich.markup import escape

    from argus.core.config import raw_settings, unknown_keys

    if cfg_path is None:
        return OK, "no settings file — defaults used"
    unknown = unknown_keys(raw_settings(cfg_path))
    if unknown:
        return WARN, escape("unknown keys (typo?): " + ", ".join(unknown))
    return OK, "all keys are known"


def _check_disk(cfg: Config) -> tuple[str, str]:
    usage = shutil.disk_usage(cfg.root)
    free_gb = usage.free / 2**30
    where = Path(cfg.root).drive or "/"
    msg = f"{free_gb:.1f} GB free on {where}"
    if free_gb < 5:
        return FAIL, msg + " — too little, the models won't fit"
    if free_gb < 15:
        return WARN, msg + " — barely enough for session recordings"
    return OK, msg


_MAC_STATUS = {"ok": OK, "warn": WARN, "fail": FAIL}


def _check_audio(cfg: Config) -> tuple[str, str]:
    if sys.platform == "darwin":
        from argus.mac import doctor

        status, text = doctor.audio(cfg)
        return _MAC_STATUS[status], text
    try:
        import pyaudiowpatch as pyaudio
    except ImportError as e:
        return FAIL, f"pyaudiowpatch cannot be imported: {e}"

    p = pyaudio.PyAudio()
    try:
        api = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        spk = p.get_device_info_by_index(api["defaultOutputDevice"])
        mic = p.get_device_info_by_index(api["defaultInputDevice"])
        loop = next(
            (d for d in p.get_loopback_device_info_generator() if spk["name"] in d["name"]),
            None,
        )
    except OSError as e:
        return FAIL, f"WASAPI is unavailable: {e}"
    finally:
        p.terminate()

    if loop is None:
        return FAIL, "loopback device not found — system audio cannot be captured"
    return OK, (
        f"microphone '{mic['name']}' {int(mic['defaultSampleRate'])} Hz | "
        f"loopback idx={loop['index']} {int(loop['defaultSampleRate'])} Hz"
    )


def _check_screen(cfg: Config) -> tuple[str, str]:
    if sys.platform == "darwin":
        from argus.mac import doctor

        status, text = doctor.screen(cfg)
        return _MAC_STATUS[status], text
    try:
        import bettercam
    except ImportError as e:
        return FAIL, f"bettercam cannot be imported: {e}"
    try:
        import cv2  # noqa: F401
    except ImportError as e:
        return FAIL, f"opencv cannot be imported: {e}"

    cam = None
    try:
        cam = bettercam.create(output_idx=cfg.screen.output_idx, output_color="BGR")
        frame = cam.grab()
        if frame is None:
            frame = cam.grab()  # DXGI returns a frame only when the picture changes
        if frame is None:
            return WARN, "capture created, but no frame arrived — the screen is static, which is normal"
        h, w = frame.shape[:2]
        size_mb = frame.nbytes / 2**20
        return OK, f"monitor {cfg.screen.output_idx}: {w}x{h}, frame {size_mb:.1f} MB"
    except Exception as e:  # noqa: BLE001 — the doctor must not crash itself
        return FAIL, f"screen capture is not working: {e}"
    finally:
        del cam


def _check_models(cfg: Config) -> tuple[str, str]:
    """Whether the weights are on disk. Their absence is not an error: they are downloaded on first
    launch.
    """
    from argus.perception.vad import VAD_MODEL_NAME

    models = cfg.models_dir
    vad_ok = (models / VAD_MODEL_NAME).exists()

    # RapidOCR keeps its weights inside its own package, not in the project's models/.
    import rapidocr

    ocr_dir = Path(rapidocr.__file__).parent / "models"
    ocr_files = list(ocr_dir.glob("*PP-OCRv5*")) if ocr_dir.exists() else []

    parts = [f"VAD {'present' if vad_ok else 'will be downloaded'}"]
    if ocr_files:
        size_mb = sum(f.stat().st_size for f in ocr_dir.glob("*.onnx")) / 2**20
        parts.append(f"OCR {size_mb:.0f} MB")
    else:
        parts.append("OCR will be downloaded (~45 MB)")

    status = OK if (vad_ok and ocr_files) else WARN
    return status, ", ".join(parts)


def _check_claude(cfg: Config) -> tuple[str, str]:
    exe = shutil.which("claude")
    if not exe:
        if cfg.brain.provider != "claude-code":
            return OK, f"not needed: the model answers through '{cfg.brain.provider}'"
        return WARN, "claude CLI not found — it is needed for the claude-code provider"
    try:
        ver = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=30, check=True
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as e:
        return WARN, f"claude is installed but does not respond: {e}"
    return OK, ver


def _check_window(cfg: Config) -> tuple[str, str]:
    """What to show the interface with.

    The check is not for this machine but for someone else's: WebView2 comes with Windows 11 out
    of the box, but on old Windows 10 builds it may be missing. This has to be learned here, not
    at the first launch on a user's machine, where an empty space opened instead of the app.
    """
    from argus.desktop import available

    ok, detail = available()
    if not ok:
        return WARN, f"{detail}; the interface will open in the browser"
    return OK, detail if cfg.ui.window else f"{detail}, but the browser is enabled (ui.window)"


def _check_compare(cfg: Config) -> tuple[str, str]:
    """Whether the pair of recognition engines is ready.

    Comparison is off by default, and that is a normal state, not a defect. But when it is
    turned on and there is no key, this has to be learned here — not half an hour into a
    recording, on finding that the second column is empty.

    The key is checked in both roles, and the second matters more than the first. As the shadow
    engine, without a key realtime simply will not come up, and that is visible at once. As the
    main one, every utterance will silently go to the fallback: there is text in the feed, it
    was recognized by the local engine, and this can only be understood from `rescued_by` in the
    recording.
    """
    import os

    name = cfg.stt.compare_with
    if "realtime-openai" in (cfg.stt.provider, name):
        var = cfg.realtime_stt.api_key_env
        if not os.environ.get(var, "").strip():
            return FAIL, f"realtime-openai: variable {var} is empty"

    if not name:
        # A single engine is a normal state of the product, not a defect: the local one has been
        # removed, and there is no second cloud one. Complaining about it at every launch would train
        # people not to read the doctor.
        if cfg.stt.provider == "realtime-openai":
            return OK, f"{cfg.stt.provider} ({cfg.realtime_stt.active_model}), single engine"
        return OK, "off (stt.compare_with)"

    if name == cfg.stt.provider:
        return FAIL, f"nothing to compare: {name} against itself"

    try:
        from argus.perception.speech import build_stt

        build_stt(cfg, name)
    except ValueError as e:
        return FAIL, str(e)
    except ImportError as e:
        return FAIL, f"{name}: missing dependency — {e}"

    if cfg.stt.provider == "realtime-openai":
        return OK, f"{cfg.stt.provider} ({cfg.realtime_stt.active_model}), fallback {name}"
    if name == "realtime-openai":
        return OK, f"{cfg.stt.provider} vs {name} ({cfg.realtime_stt.active_model})"

    return OK, f"{cfg.stt.provider} vs {name}"


@app.command()
def doctor() -> None:
    """Check that the environment is ready: hardware, audio, screen, config."""
    cfg_path = find_config()
    try:
        cfg = Config.load(cfg_path)
    except Exception as e:  # that is what the doctor should report, not crash on
        from rich.markup import escape

        console.print(f"\n[red]settings cannot be read: {escape(str(e))}[/red]\n")
        raise typer.Exit(code=1) from e

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("")
    table.add_column("check", style="bold")
    table.add_column("result")

    checks = [
        ("Python", _run_check(_check_python)),
        ("Settings", _run_check(_check_settings, cfg_path)),
        ("Disk", _run_check(_check_disk, cfg)),
        ("Audio", _run_check(_check_audio, cfg)),
        ("Screen", _run_check(_check_screen, cfg)),
        ("Models", _run_check(_check_models, cfg)),
        ("Claude", _run_check(_check_claude, cfg)),
        ("Comparison", _run_check(_check_compare, cfg)),
        ("Window", _run_check(_check_window, cfg)),
    ]
    for label, (status, detail) in checks:
        table.add_row(status, label, detail)

    console.print()
    console.print(table)
    console.print()
    console.print(f"  config: {cfg_path or 'not found, defaults used'}")
    console.print(f"  recordings: {cfg.sessions_dir}")
    console.print(f"  models: {cfg.models_dir}")
    console.print()

    if any(status == FAIL for _, (status, _) in checks):
        raise typer.Exit(code=1)


@app.command()
def capture(
    seconds: float = typer.Option(0, "--seconds", "-s", help="How long to run; 0 - until Ctrl+C."),
    snapshot: bool = typer.Option(True, help="Save a JPEG screenshot once per second."),
) -> None:
    """Show live audio levels and the frame rate: acceptance of the capture."""
    import asyncio

    from argus.preview import run_capture

    cfg = Config.load()
    snap_path = (cfg.sessions_dir / "preview.jpg") if snapshot else None
    try:
        code = asyncio.run(run_capture(cfg, seconds, snap_path))
    except KeyboardInterrupt:
        code = 0
    except DeviceNotFound as e:
        console.print(f"\n[red]{e}[/red]\n")
        raise typer.Exit(code=1) from e
    if code:
        raise typer.Exit(code=code)


@app.command()
def record(
    seconds: float = typer.Option(0, "--seconds", "-s", help="How long to record; 0 - until Ctrl+C."),
    screen: bool = typer.Option(True, help="Record screen frames. --no-screen - audio only."),
) -> None:
    """Record a session to disk so it can be run through the pipeline later."""
    import asyncio

    from argus.preview import run_capture

    cfg = Config.load()
    try:
        code = asyncio.run(
            run_capture(cfg, seconds, snapshot=None, record=True, with_screen=screen)
        )
    except KeyboardInterrupt:
        code = 0
    except DeviceNotFound as e:
        console.print(f"\n[red]{e}[/red]\n")
        raise typer.Exit(code=1) from e
    if code:
        raise typer.Exit(code=code)


@app.command()
def listen(
    seconds: float = typer.Option(0, "--seconds", "-s", help="How long to listen; 0 - until Ctrl+C."),
    screen: bool = typer.Option(True, help="Also capture the screen."),
) -> None:
    """Listen and recognize speech in real time."""
    import asyncio

    from argus.listen import run_listen

    cfg = Config.load()
    try:
        code = asyncio.run(run_listen(cfg, seconds, with_screen=screen))
    except KeyboardInterrupt:
        code = 0
    except DeviceNotFound as e:
        console.print(f"\n[red]{e}[/red]\n")
        raise typer.Exit(code=1) from e
    if code:
        raise typer.Exit(code=code)


@app.command()
def transcribe(
    wav: Path = typer.Argument(..., help="WAV 16 kHz mono."),  # noqa: B008 — typer requires it
) -> None:
    """Recognize a finished WAV: a model check without a microphone."""
    import asyncio

    from rich.markup import escape

    from argus.listen import run_transcribe
    from argus.perception.speech import BadWavError

    if not wav.exists():
        console.print(f"\n[red]file not found: {wav}[/red]\n")
        raise typer.Exit(code=1)
    try:
        asyncio.run(run_transcribe(Config.load(), wav))
    except BadWavError as e:
        # Wrong format or not a WAV at all: a plain phrase and exit code 1 instead of a traceback.
        console.print(f"\n[red]{escape(str(e))}[/red]\n")
        raise typer.Exit(code=1) from e


def _tts_api_key() -> str:
    import os

    from argus.tts.settings import TTSSettings

    settings = TTSSettings()  # the environment variable's key is the default, we do not read the config
    api_key = os.environ.get(settings.api_key_env, "").strip()
    if not api_key:
        console.print(f"\n[red]{settings.api_key_env} is empty - put the key in .env[/red]\n")
        raise typer.Exit(code=1)
    return api_key


RESTART_HINT = (
    "  if Argus is already running, restart it: while it is open, new voices do not show up on the "
    "settings page\n"
)


def _save_character(
    cfg: Config, voice_id: str, name: str, *, language: str = "", preview_url: str = "",
    hint: bool = True,
) -> str:
    """Create a character on `voice_id` in `channel.tts.characters` — the shared tail of
    `clone-voice`, `add-voice`, `sync-voices` and `import-voice`: the commands differ, but the
    write to the config is one.

    The slug is Latin/digits/Cyrillic (`\\w` in Python is Unicode by default), spaces and
    everything else become a hyphen; otherwise a purely Cyrillic name (say, the Russian for "Test") would be cut down to an empty
    string and fall back to the faceless "voice".
    """
    import re

    from argus.core.settings_store import SettingsStore, deep_merge, get_path

    slug = re.sub(r"[^\w]+", "-", name.lower()).strip("-") or "voice"

    # A fresh `SettingsStore` reads `argus.local.json` from disk anew in the constructor
    # (`read_overrides`) — unlike `cfg`, which stays the same snapshot it was at `Config.load()`.
    # This matters when, within one run (`sync-voices` looping over voices), this function is
    # called many times in a row with the same `cfg`: a collision check against `cfg` would not see
    # a namesake created at the previous step of the same loop — but against the disk it does,
    # because the previous call has already written there.
    store = SettingsStore(cfg)
    current = deep_merge(store.base_raw, store.overrides)
    taken_voice_id = get_path(current, f"channel.tts.characters.{slug}.voice_id")
    if taken_voice_id and taken_voice_id != voice_id:
        # Namesakes are not hypothetical: two different voices in the ElevenLabs library were found,
        # both literally named "Hoffmann" (written in Cyrillic). Silently overwriting someone else's `voice_id`
        # under the same key would mean losing one of them from the config without a single warning —
        # the id tail separates the slugs without touching the namesake that was already created.
        slug = f"{slug}-{voice_id[:4].lower()}"

    patch = {
        f"channel.tts.characters.{slug}.voice_id": voice_id,
        f"channel.tts.characters.{slug}.label": name,
    }
    if language:
        patch[f"channel.tts.characters.{slug}.language"] = language
    if preview_url:
        patch[f"channel.tts.characters.{slug}.preview_url"] = preview_url
    store.apply(patch)
    console.print(f"  character key: [bold]{slug}[/bold]")
    console.print(
        f"  written to {cfg.root / 'argus.local.json'} - enter '{slug}' in "
        f"'Character' on the settings page and turn on voice-over\n"
    )
    if hint:
        console.print(RESTART_HINT)
    return slug


@app.command("clone-voice")
def clone_voice(
    wav: Path = typer.Argument(..., help="Voice sample - 30-60 sec of clean speech."),  # noqa: B008
    name: str = typer.Argument(..., help="Character name."),
    save: bool = typer.Option(
        True, help="Write voice_id to channel.tts.characters (argus.local.json)."
    ),
) -> None:
    """Clone a voice in ElevenLabs (Instant Voice Cloning): a test feature."""
    import asyncio

    from argus.tts import client as tts_client

    if not wav.exists():
        console.print(f"\n[red]file not found: {wav}[/red]\n")
        raise typer.Exit(code=1)

    # `Config.load()` before `_tts_api_key()`, not after: it also pulls in `.env` (see `load_env`
    # in `core/config.py`) — in the reverse order the command in a clean process (without the
    # environment variable already set) fails with the "is empty" key error even when `.env` is
    # in place.
    cfg = Config.load()
    api_key = _tts_api_key()
    try:
        voice_id = asyncio.run(
            tts_client.add_voice(name, wav.read_bytes(), wav.name, api_key)
        )
    except Exception as e:
        console.print(f"\n[red]cloning failed: {e}[/red]\n")
        raise typer.Exit(code=1) from e

    console.print(f"\n  voice_id: [bold]{voice_id}[/bold]")
    if save:
        _save_character(cfg, voice_id, name)
    else:
        console.print()


@app.command("add-voice")
def add_voice_cmd(
    voice_id: str = typer.Argument(..., help="voice_id - your own clone or a clone by someone else, already added to 'my voices'."),
    name: str = typer.Argument(..., help="Character name."),
) -> None:
    """Create a character on an already existing voice_id — without cloning.

    For a voice from the ElevenLabs library (a page like elevenlabs.io/voices/<id>): first press
    "Use voice" / "Add to my voices" on that page in your own account. Without this step the
    synthesis will return a refusal — ElevenLabs does not hand out other people's voices through
    the API without an explicit addition to your own library, even if you know the id.

    A voice from the shared library is added the same way by `import-voice` — already through
    the API, without a trip to the site by hand.
    """
    _save_character(Config.load(), voice_id, name)


def _voice_language(voice: dict) -> str:
    """The voice language from an ElevenLabs response: the field sits in different places for
    premade/own voices (`fine_tuning.language`) and for the shared library (`language` in the
    record itself)."""
    # `fine_tuning: null` in the response is not "no field": `.get(key, {})` would return `None`.
    fine = voice.get("fine_tuning")
    language = fine.get("language") if isinstance(fine, dict) else None
    return str(language or voice.get("language") or "")


@app.command("sync-voices")
def sync_voices() -> None:
    """Create characters for everything already available to the ElevenLabs account.

    Premade voices, your own clones, things bought on the marketplace — whatever
    `GET /v1/voices` returns can be voiced right now, without a single request to add it. It is
    free and does not touch the account — unlike `import-voice`. Those already created (by
    `voice_id`) are skipped, not overwritten: a person may have edited the character's name and
    language in the config.
    """
    import asyncio

    from argus.tts import client as tts_client
    from argus.tts.settings import read as tts_read

    # `Config.load()` before `_tts_api_key()` — it also pulls in `.env` (see the `clone_voice` fix
    # above and `load_env` in `core/config.py`).
    cfg = Config.load()
    api_key = _tts_api_key()
    existing = {p.voice_id for p in tts_read(cfg).characters.values()}

    try:
        voices = asyncio.run(tts_client.list_voices(api_key))
    except Exception as e:
        console.print(f"\n[red]failed to get the voice list: {e}[/red]\n")
        raise typer.Exit(code=1) from e

    added = 0
    for voice in voices:
        voice_id = voice.get("voice_id")
        name = voice.get("name") or voice_id
        if not voice_id or voice_id in existing:
            continue
        _save_character(
            cfg, voice_id, name,
            language=_voice_language(voice), preview_url=voice.get("preview_url") or "",
            hint=False,
        )
        added += 1

    console.print(
        f"[bold]{added}[/bold] new characters out of {len(voices)} voices "
        f"in the account (already existing ones skipped: {len(voices) - added})\n"
        if voices else "no voices found in the account\n"
    )
    if added:
        console.print(RESTART_HINT)


@app.command("browse-voices")
def browse_voices(
    language: str = typer.Option("", "--language", "-l", help="Language code, e.g. ru, en, uk."),
    gender: str = typer.Option("", "--gender", "-g", help="male / female."),
    search: str = typer.Option("", "--search", "-s", help="Substring in the name/description."),
    limit: int = typer.Option(20, "--limit", "-n", help="How many to show."),
) -> None:
    """Browse the shared ElevenLabs voice library — see what is there before adding anything to the
    account (`import-voice`).

    Read-only: it does not touch the account and does not spend slots.
    """
    import asyncio

    from argus.tts import client as tts_client

    # We do not create a character — `Config` is not needed here for anything of its own, but
    # loading pulls in `.env` as a side effect anyway, and without it a bare key from the
    # environment is visible only if it was set by hand in the terminal.
    Config.load()
    api_key = _tts_api_key()
    try:
        voices = asyncio.run(tts_client.search_shared_voices(
            api_key, language=language, gender=gender, search=search, page_size=limit,
        ))
    except Exception as e:
        console.print(f"\n[red]failed to get the voice library: {e}[/red]\n")
        raise typer.Exit(code=1) from e

    if not voices:
        console.print("\nnothing found for this filter\n")
        return

    # `no_wrap` on the last two: that is exactly what gets copied into `import-voice` — an id cut
    # down to "…" cannot be copied, and without `no_wrap` Rich squeezes the widest columns first,
    # not the least important ones, and would cut exactly them to make room for the "name".
    table = Table(show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("language")
    table.add_column("gender")
    table.add_column("accent")
    table.add_column("voice_id", no_wrap=True)
    table.add_column("public_owner_id", no_wrap=True)
    for v in voices:
        table.add_row(
            v.get("name", ""), v.get("language", ""), v.get("gender", ""),
            v.get("accent", ""), v.get("voice_id", ""), v.get("public_owner_id", ""),
        )
    console.print(table)
    console.print(
        "\nto create a character: [bold]argus import-voice "
        "<public_owner_id> <voice_id> <name>[/bold]\n"
    )


@app.command("import-voice")
def import_voice(
    public_owner_id: str = typer.Argument(..., help="public_owner_id from `argus browse-voices`."),
    voice_id: str = typer.Argument(..., help="voice_id from `argus browse-voices`."),
    name: str = typer.Argument(..., help="Character name."),
    language: str = typer.Option("", "--language", "-l", help="Language code - if not set, taken from the library."),
) -> None:
    """Add a voice from the shared ElevenLabs library to the account and create a character — with
    one command, without a trip to the site by hand (see `add-voice`).

    Unlike `sync-voices`, it spends a custom-voice slot in the account — their number is limited
    by the ElevenLabs plan.
    """
    import asyncio

    from argus.tts import client as tts_client

    cfg = Config.load()
    api_key = _tts_api_key()
    try:
        voice_id = asyncio.run(tts_client.add_shared_voice(public_owner_id, voice_id, name, api_key))
    except Exception as e:
        console.print(f"\n[red]failed to add the voice: {e}[/red]\n")
        raise typer.Exit(code=1) from e

    # `add_shared_voice` returns only `voice_id` in the response — we get a fresh sample with a
    # separate `list_voices()`: the voice has just been added to the account, so it is already
    # there and already carries `preview_url` (see `VoiceProfile.preview_url` — the admin page's
    # "Play a sample" plays it for free, without paid synthesis).
    preview_url = ""
    try:
        for voice in asyncio.run(tts_client.list_voices(api_key)):
            if voice.get("voice_id") == voice_id:
                preview_url = voice.get("preview_url", "")
                break
    except Exception:  # noqa: BLE001 — an optional step, the voice is already added and will be saved without it
        pass

    console.print(f"\n  voice_id: [bold]{voice_id}[/bold]")
    _save_character(cfg, voice_id, name, language=language, preview_url=preview_url)


@app.command()
def favorite(
    slug: str = typer.Argument(..., help="Character key - the one printed by `clone-voice`/`sync-voices`."),
    off: bool = typer.Option(False, "--off", help="Remove from favorites instead of adding."),
) -> None:
    """Move a character to the top of the list in the admin panel: the same as the star on the voice
    card on the settings page, only from the terminal."""
    from argus.core.settings_store import SettingsStore
    from argus.tts.settings import read as tts_read

    cfg = Config.load()
    if slug not in tts_read(cfg).characters:
        console.print(f"\n[red]character '{slug}' does not exist - `argus sync-voices` lists them[/red]\n")
        raise typer.Exit(code=1)

    SettingsStore(cfg).apply({f"channel.tts.characters.{slug}.favorite": not off})
    console.print(f"\n  {slug}: favorite - [bold]{'no' if off else 'yes'}[/bold]\n")
    console.print(RESTART_HINT.replace("new voices", "new mark"))


def _landing_path(cfg: Config) -> str:
    """Where to open the page at launch: see `argus.landing.landing_path`.

    The function itself lives there: the built app calls it without loading this whole module
    (`typer`, `rich`)."""
    from argus.landing import landing_path

    return landing_path(cfg)


@app.command()
def run(
    port: int = typer.Option(
        0, help="Dashboard port; 0 - take from settings (ui.port)."
    ),
    screen: bool = typer.Option(True, help="Capture the screen."),
    brain: bool = typer.Option(True, help="Connect the model."),
    record: bool = typer.Option(False, "--record", help="Also write the session to disk."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the browser."),
    window: bool = typer.Option(
        None, "--window/--browser",
        help="Show its own window or open the browser; by default taken from settings (ui.window).",
    ),
    start: bool = typer.Option(
        False, "--start", help="Turn on recognition immediately, without waiting for the button."
    ),
    seconds: float = typer.Option(
        0, "--seconds", "-s",
        help="Stop on its own this many seconds after recording starts (without --record, after "
             "recognition is turned on); 0 - until Ctrl+C. If the requested recording did not open, "
             "recognition is not turned on and the run ends.",
    ),
    settings: bool = typer.Option(
        False,
        "--settings",
        help="Settings only: no audio, screen or models. This way three copies "
             "can be open at the same time.",
    ),
) -> None:
    """Bring up the dashboard. Recognition is switched on by a button in it."""
    import asyncio

    from argus.runner import run_app

    cfg = Config.load()
    as_window = cfg.ui.window if window is None else window
    landing_path = _landing_path(cfg)

    def check_update() -> bool:
        # The window's mutex must be taken before the update check (where there is a window at all) —
        # otherwise a repeated launch while the first copy is already running downloaded and unpacked
        # the archive over the running program every time. In browser mode there is no mutex at all —
        # we check as before, immediately.
        from argus.update import maybe_self_update

        return maybe_self_update(cfg.update.server, cfg.root)

    if settings:
        from argus.settings_only import run_settings
        from argus.ui.server import has_admin

        if as_window:
            from argus.desktop import claim_instance_or_focus, run_desktop

            if not claim_instance_or_focus(cfg, port):
                return
            if check_update():
                return
            raise typer.Exit(code=run_desktop(
                cfg,
                lambda stop: run_settings(cfg, port, False, stop),
                port,
                path="/admin" if has_admin(cfg) else "",
                claimed=True,
            ))
        if check_update():
            return
        try:
            asyncio.run(run_settings(cfg, port, open_browser))
        except KeyboardInterrupt:
            pass
        return

    # The window is the usual way to launch. The browser stays for debugging: it has developer
    # tools and an address bar, which the window does not.
    if as_window:
        from argus.desktop import claim_instance_or_focus, run_desktop

        if not claim_instance_or_focus(cfg, port):
            return
        if check_update():
            return
        try:
            code = run_desktop(
                cfg,
                lambda stop: run_app(
                    cfg, port, screen, brain, record,
                    open_browser=False, autostart=start, seconds=seconds,
                    stop_signal=stop,
                ),
                port,
                path=landing_path,
                claimed=True,
            )
        except KeyboardInterrupt:
            return
        except DeviceNotFound as e:
            console.print(f"\n[red]{e}[/red]\n")
            raise typer.Exit(code=1) from e
        raise typer.Exit(code=code)

    if check_update():
        return
    try:
        asyncio.run(run_app(
            cfg, port, screen, brain, record, open_browser,
            open_path=landing_path, autostart=start, seconds=seconds,
        ))
    except KeyboardInterrupt:
        pass
    except DeviceNotFound as e:
        console.print(f"\n[red]{e}[/red]\n")
        raise typer.Exit(code=1) from e


@app.command()
def sessions() -> None:
    """Show the recorded sessions."""
    from argus.session.layout import dir_size, list_sessions

    cfg = Config.load()
    found = list_sessions(cfg.sessions_dir)
    if not found:
        console.print(f"\n  no recordings in {cfg.sessions_dir}\n")
        return

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    for col in ("recording", "duration", "frames", "size", "devices"):
        table.add_column(col)
    for path, meta in found:
        devices_note = " / ".join(
            d.name for d in (meta.mic, meta.loopback) if d is not None
        )
        table.add_row(
            path.name,
            f"{meta.duration_s:.1f} s",
            str(meta.frames),
            f"{dir_size(path) / 2**20:.1f} MB",
            devices_note,
        )
    console.print()
    console.print(table)
    console.print()


@app.command()
def replay(
    session: str = typer.Argument(..., help="Recording name or its tail, e.g. 1433."),
    speed: float = typer.Option(0, "--speed", help="1 - real time, 0 - as fast as possible."),
    frames: bool = typer.Option(True, help="Read screen frames (slower)."),
    stt: bool = typer.Option(False, "--stt", help="Also recognize speech."),
    ocr: bool = typer.Option(False, "--ocr", help="Also read the screen."),
) -> None:
    """Run a recording through the pipeline as if it were happening live."""
    import asyncio

    from argus.session.replay_run import run_replay

    cfg = Config.load()
    try:
        asyncio.run(run_replay(cfg, session, speed, frames, with_stt=stt, with_ocr=ocr))
    except (FileNotFoundError, ValueError) as e:
        console.print(f"\n[red]{e}[/red]\n")
        raise typer.Exit(code=1) from e
    except KeyboardInterrupt:
        pass


@app.command()
def devices() -> None:
    """Show all audio devices with their indexes (WASAPI on Windows, CoreAudio on macOS)."""
    if sys.platform == "darwin":
        from argus.mac import doctor

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        for col in ("idx", "device", "in", "out", "Hz", "type"):
            table.add_column(col)
        for row in doctor.device_rows():
            table.add_row(*row)
        console.print()
        console.print(table)
        console.print()
        return

    import pyaudiowpatch as pyaudio

    p = pyaudio.PyAudio()
    try:
        api = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_in = api["defaultInputDevice"]
        default_out = api["defaultOutputDevice"]

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        for col in ("idx", "device", "in", "out", "Hz", "type"):
            table.add_column(col)

        for idx in range(p.get_device_count()):
            d = p.get_device_info_by_index(idx)
            if d["hostApi"] != api["index"]:
                continue  # WASAPI only: the other APIs duplicate the same devices
            kind = []
            if d.get("isLoopbackDevice"):
                kind.append("[cyan]loopback[/cyan]")
            if idx == default_in:
                kind.append("[green]default input[/green]")
            if idx == default_out:
                kind.append("[green]default output[/green]")
            table.add_row(
                str(idx),
                d["name"],
                str(d["maxInputChannels"]),
                str(d["maxOutputChannels"]),
                str(int(d["defaultSampleRate"])),
                " ".join(kind),
            )
        console.print()
        console.print(table)
        console.print()
    finally:
        p.terminate()


@app.command("account-server")
def account_server(
    dev: bool = typer.Option(
        False, "--dev",
        help="Local run for development: its own stub payment gateway and mail "
             "to the console, the secret is created automatically and stored next to the database. Listens only on "
             "this computer; on the network only with ARGUS_ALLOW_STUB=1 (the stub tops up "
             "the balance without payment).",
    ),
    host: str = typer.Option("127.0.0.1", help="Address to listen on."),
    port: int = typer.Option(8421, help="Port."),
) -> None:
    """Bring up the account server — a separate process, not part of the desktop app.

    In production the address, the secret and the payment gateway come from the environment
    (`ARGUS_SERVER_SECRET`, `ARGUS_SERVER_DB`, `ARGUS_BILLING`, ...) — see `server/app.py:from_env`.
    `--dev` brings up the same thing locally: its own SQLite next to the project, a stub payment
    gateway (it tops up the balance without payment), and mail goes to the log instead of a
    mailbox. After that, put this address into `[account].server` in `argus.toml` so that login
    and balance start working.
    """
    import secrets as secrets_

    import uvicorn

    local_only = host.strip().lower() in ("127.0.0.1", "localhost", "::1")
    if dev and not local_only:
        # A stub payment gateway tops up the balance without payment: opened to the network, it would
        # become a free money giveaway to anyone who knows the address. The same lock as on the
        # production server (`billing.from_env`): without an explicit `ARGUS_ALLOW_STUB=1` the stub
        # does not go to the network. It is needed by those who run the server in a virtual machine or
        # a container: there it has to listen on 0.0.0.0, while it is accessed from the host machine.
        if os.environ.get("ARGUS_ALLOW_STUB", "").strip() != "1":
            console.print(
                "\n[red]--dev is for this computer only: the stub payment gateway tops up the balance "
                "without payment. Remove --host or set 127.0.0.1. If the server really "
                "is needed on the developer network (virtual machine, container), set "
                "ARGUS_ALLOW_STUB=1.[/red]\n"
            )
            raise typer.Exit(code=1)
        console.print(
            f"\n[bold red]STUB PAYMENT GATEWAY IS OPEN TO THE NETWORK ({host}): anyone who knows the address "
            "can top up their balance without payment. Only for a closed development network.[/bold red]\n"
        )

    if dev:
        from argus.server import billing, mailer
        from argus.server.app import build
        from argus.server.db import connect

        secret_path = Path("argus-accounts.secret")
        if secret_path.exists():
            secret = secret_path.read_text().strip()
        else:
            secret = secrets_.token_urlsafe(32)
            secret_path.write_text(secret)

        db = connect(Path("argus-accounts.sqlite"))
        web = build(db, secret, pay=billing.Stub(), mail=mailer.Console())
        console.print(
            "[yellow]dev mode: stub payment gateway, mail to console, "
            "secret is argus-accounts.secret (not committed to the repository)[/yellow]"
        )
    else:
        from argus.server.app import from_env

        try:
            web = from_env()
        except SystemExit as e:
            console.print(f"\n[red]{e}[/red]\n")
            raise typer.Exit(code=1) from None

    console.print(f"  accounts server: [bold cyan]http://{host}:{port}[/bold cyan]")
    console.print("  [dim]Ctrl+C to stop[/dim]\n")
    uvicorn.run(web, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
