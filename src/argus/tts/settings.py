"""Voice-over settings. They live in `channel.tts`; the core does not parse them.

Off by default: synthesis is paid per character, and this is a test feature. It must
be turned on deliberately, not silently together with an update.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class VoiceProfile(BaseModel):
    """One "character": a cloned voice plus the way its sound is tuned.

    `voice_id` is obtained through `argus clone-voice` (ElevenLabs Instant Voice Cloning)
    and is not written here by hand. The command puts it into
    `channel.tts.characters.<key>` itself.
    """

    label: str = ""
    voice_id: str = ""

    # ElevenLabs voice parameters. 0 is more emotional, but it "wanders" between generations
    # (the same character sounds different from run to run); 1 is more monotone and
    # predictable. 0.5 (their own default) gives a noticeable spread on an instant clone;
    # 0.65 is steadier without losing expressiveness entirely. Tune it by ear; this is not
    # a physical constant.
    stability: float = Field(default=0.65, ge=0.0, le=1.0)
    similarity_boost: float = Field(default=0.8, ge=0.0, le=1.0)

    # Taken from ElevenLabs metadata (`fine_tuning.language` for premade/own voices,
    # `language` for voices from the shared library), or set by hand on import. Empty means
    # unknown or a multilingual voice. It does not affect synthesis: `eleven_multilingual_v2`
    # will voice any text with any voice. The tag is needed only for the filter and the
    # label in the admin page.
    language: str = ""

    # A ready-made sample from ElevenLabs: a static mp3 at a direct link, with no key and no
    # use of the per-character quota (checked live: both account voices and shared-library
    # voices carry it). The "Play a sample" button in the admin page plays THIS sample
    # instead of running paid synthesis (`POST /tts/preview`) for a test phrase. Only
    # characters created by hand through `clone-voice`/`add-voice` before this field existed
    # lack it. It is empty for them, and the widget falls back to the paid path, as before.
    preview_url: str = ""

    # Pinned to the top of the list in the admin page. There may be several dozen characters,
    # not three (premade ElevenLabs voices alone number more than twenty, not counting the
    # shared library), and without this people would have to search for the one they need
    # among all of them every time.
    favorite: bool = False


class TTSSettings(BaseModel):
    enabled: bool = False

    # A key in `characters`. Empty means no voice-over, even if `enabled = true`: there is
    # nothing to synthesize with the voice of a "no" character.
    active_character: str = ""

    # `eleven_flash_v2_5` (fast, ~75 ms) was the default at first, for the sake of a fast
    # first sound. In practice, on Russian text it noticeably hurt the naturalness of
    # intonation and stress compared with the preview on the ElevenLabs site (which runs the
    # flagship model). Since quality matters more than a couple of hundred milliseconds in a
    # test, the default was switched to the flagship. Flash remains available as
    # `channel.tts.model_id`, in case speed matters more again later.
    model_id: str = "eleven_multilingual_v2"
    output_format: str = "mp3_44100_128"

    # A cap on the length of a voiced reply. It is not the same as
    # `assist.answering.max_answer_chars`: that one is about what is read by eye in three
    # seconds, this one is about what is paid to ElevenLabs per character.
    max_chars: int = Field(default=500, ge=50, le=2000)

    # The key comes from the environment (`.env`), not from the config: the config file is
    # in the repository.
    api_key_env: str = "ELEVENLABS_API_KEY"

    # Synthesis goes through the account server (`server/relay.py`), not with a key on this
    # computer. This is how the build is distributed: `ELEVENLABS_API_KEY` is then not needed
    # in the app's `.env` at all; it exists only on the server.
    relay: bool = False

    # Characters are a dict, not a list: a point edit of one through
    # `channel.tts.characters.<key>.voice_id` must merge with the others, and `deep_merge` in
    # `core/settings_store.py` merges only dicts. A list would be patched as a whole.
    characters: dict[str, VoiceProfile] = Field(default_factory=dict)


KEY = "tts"


def read(cfg) -> TTSSettings:
    """Extract the settings from the free-form section of the config.

    Bad values do not crash startup: the pipeline stays without voice-over, on defaults,
    instead of failing. This is a test feature, not something Argus is useless without.
    """
    import logging

    try:
        return TTSSettings.model_validate(cfg.channel.get(KEY, {}))
    except Exception:
        logging.getLogger(__name__).exception("voice settings are invalid: using the defaults")
        return TTSSettings()
