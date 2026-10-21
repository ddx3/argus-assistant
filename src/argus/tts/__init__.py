"""Speech synthesis of answers is an experimental feature, separate from the core and from
`assist`.

A voice cloned from a sample (ElevenLabs), not a fixed synthesizer: you give the model a
piece of someone's voice, and from then on the answers sound in it.

It is plugged in with one call, `tts.reply.install(app, settings)`, and the same call
unplugs it. The core (`brain/assistant.py`, `app.py`) does not know this module and must
not: it listens to answers that are already published, and does not stand in their way.
"""
