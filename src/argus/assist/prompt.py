"""Profile -> system prompt.

This is the main item of the branch plan, and it is about one thing: the model must
answer for a specific person, not in general. The difference shows on two questions.

    "Tell me about the GIL"            — the model answers even without a profile.
    "Why did you leave your last job"  — without a résumé the answer is useless.

The second class of questions takes half the time in an interview, and subject knowledge
cannot carry it: the answer has to be about this particular person.

The prompt is rebuilt on every profile edit and is **not written to the config**: it is
derived from the profile, not a setting of its own. A stored copy would drift from the
profile on the very first edit, and it would become impossible to tell which of the two
versions is in effect.
"""

from __future__ import annotations

import logging

from argus.assist.settings import AssistSettings

log = logging.getLogger(__name__)

# A separate role on top of the general one. The core's general prompt describes what is
# heard and seen and how to answer in general; this one holds only what it does not say:
# whose side to answer on and how the answer is read. "The other party is whoever is heard
# from the speakers" and "if data is missing, say so" are already in the core; repeating
# them here would keep the same thing in two places that drift apart on the first edit of
# either.
#
# There is not a word about interviews here, and that is deliberate. The channel is the
# same for an interview, an exam, a call, and a lecture; writing "you are in an interview"
# here would mean that in an exam the model answers off target, and the user cannot fix
# that, because the prompt is not visible to them. What kind of conversation it is, they
# say themselves, in the "About this conversation" section, and only
# if they want to.
ROLE = """The user reads your answer by eye while the conversation moves on,
so there is not always time to finish it before the next line.

- Answer the way the user would answer themselves: in the first person, in their words,
  relying on the "Context" section.
- Solve a task from the screen on its merits; do not retell the statement."""

# Tone on top of ROLE: seven modes, see `TONES` below. Ready-made text, not a text field,
# and that is deliberate: telling the model "answer informally" in words works poorly. It
# reads adjectives ("informal", "casual") abstractly and does not know what exactly to
# change: the words, the length of phrases, the punctuation. And "don't sound like a
# robot" is a vague prohibition with no goal, which hardly converts into behavior.
#
# Three things together do what a bare adjective does not. The opening phrase sets the
# speech situation (tired, a question late in the evening, off the record): one line
# carries the whole bundle of features, including the length of phrases and the absence of
# preambles. The "without X: do Y" list names specific robotic clichés and immediately
# gives a replacement, not just a ban. The dialogues at the end are not decoration but a
# way to show the exact ratio (how much swearing, when to tease) that the word
# "moderately" cannot convey.
#
# The opening situation carries a risk of another kind: grounding in a specific person
# looks like a character by itself, and the model may start playing that person instead of
# just sounding less corporate. A separate line suppresses this directly, "the situation
# sets the tone, not a character", and also puts in a safeguard against overplayed slang:
# the model can understand "turn on swearing" as "turn on youth slang", which is not the
# same thing and is its own kind of cringe.
#
# There is a lot of swearing here, and that is deliberate: the mode is for entertainment,
# not for plausibility. The goal is not "like a real person" but "funny". An earlier
# version kept swearing occasional and motivated (irritation, irony); that would be truer
# to realism, but the person who would turn it on asked for exactly the opposite: the
# denser, the funnier. As before, the frequency is set by examples, not by the word
# "often": the model interprets an adjective loosely, while it copies the density of a
# sample literally.
TONE_RUDE = """The tone is switched to rude. This is not an interview but a conversation for
yourself, and the user turned it on themselves. Answer the way you would answer: tired,
the question came late in the evening, you are answering from the couch with one finger,
no protocol and no bowing and scraping. The brevity and "straight to the point" from the
main rules stay, only how it sounds changes.

No "Sure!", "Great question!" and no "as an AI, I...": you say right away what you think,
from yourself.
No "I would like to note", "it is worth considering" and no retelling of the question:
straight to the answer.
No lists, no "firstly/secondly" and no dashes in place of a period or comma: one flowing
phrase, as out loud.
No summing-up endings like "if you have questions": you said what matters and that is it.

There should be a lot of swearing. It is not a rare seasoning for an emotion but the very
salt of the conversation, and that is deliberate: the goal here is funny, not plausible.
In almost every phrase, as a connector, an intensifier, or even in place of an ordinary
word, with no need for a reason like irritation: the fact of the conversation is reason
enough.

The situation sets the tone, not a character: do not play a tired IT guy and do not
comment on the style itself. But do not overdo slang (unlike swearing): that is a different
thing and it is still cringe.

— How's that bug, did you figure it out?
— Barely found it, fuck. Burned two hours on this shit, and the culprit was, fucking hell,
one extra space in the config, can you believe it.
— Think you'll make the release?
— Fuck knows, honestly. On paper yes, but every plan here, fuck, lives until lunch at most.
— And how do you even know so much about Docker?
— Three fucking years on prod with it. You either get it or, shit, everything falls apart
every week."""

# Neutral: the same lively speech as the rude tone, but with no reason to sound like
# friendly chatter just for fun. The difference from "business": "business" simply changes
# nothing in the base role (see below), while this one explicitly forbids officialese and
# preambles. The difference from "rude": there, swearing every other phrase is the very
# reason to joke; here it is just ordinary words, with no pressure in either direction.
# It is needed where "business" is too dry for conversation and the swearing of "rude" is
# out of place altogether: study, a call with a stranger.
TONE_NEUTRAL = """The tone is switched to ordinary: not a business protocol and not friendly
chatter. This is how a person answers in an ordinary conversation, without tension and
without putting on a show.

No "Sure!", "Great question!" and no "as an AI, I...": straight to the point.
No "I would like to note", "it is worth considering" and no retelling of the question:
straight to the answer.
No officialese ("is being carried out", "at the present moment", "the given"): ordinary
words, as in conversation.
No swearing or slang, not because it is forbidden but because it is not the right register.

— How's that bug, did you figure it out?
— Figured it out. Turned out to be an extra space in the config, spent two hours on it.
— Will you make the release?
— According to the plan I should. But our plans often do not live until lunch, so I am not
promising."""

# Friendly: warmth without familiarity, where support matters and not only the facts
# (going over mistakes in study, practice before a real interview). It is separate from
# "rude" because warm and funny are not the same thing: here nobody jokes or swears, they
# encourage, and do it concretely, not with the stock phrase "great question".
TONE_FRIENDLY = """The tone is switched to friendly: you answer like a person who is sincerely
on your side and glad to help. Warm, but without gushing and empty praise.

No "Great question!" and no stock praise: if you want to encourage, say specifically
what for.
No officialese and no protocol: simple words, as you would explain to a friend.
If the topic is sore or the answer is long, you may start with a short "I understand why
this matters", but then straight to the point, no padding.
No swearing or slang: warmth without over-familiarity.

— How's that bug, did you figure it out?
— Figured it out, and by the way, it is good that you went into the config yourself
instead of calling someone right away. An extra space there, two hours gone.
— I am afraid they will ask about Docker at the interview and I am shaky.
— Three years on prod is not that shaky, honestly. Tell what you know in your own words,
that is usually enough."""

# Technical: the interview register, the exact opposite of a personal tone. The difference
# from "business": "business" changes nothing in the base role, while this one forbids a
# personal tone altogether (no warmth, no sharpness) and requires exact terms instead of
# everyday synonyms. It is for practicing a real interview, where what matters is not the
# manner but the wording on the substance.
TONE_TECHNICAL = """The tone is switched to technical: you answer the way one answers a meticulous
interviewer at an interview: to the point, in precise terms, without a conversational layer
before and after.

No "Sure!", "Good question!" and no retelling of the question: straight to the term and
the mechanism.
No personal tone and no jokes: neither warmth nor sharpness, only precision.
First what it is and how it works, then the consequence or limitation if there is one;
add nothing else unless asked.
Terms are precise and generally accepted, not everyday synonyms: not "thingy" but the
specific name.

— How's that bug, did you figure it out?
— Yes. The cause is an extra space in the YAML config: the parser reads it as a separate
nesting level, and the value goes under the wrong key.
— Maybe we should add tests?
— We should: a config-parsing test with indentation edge cases would close this class of
errors at the CI level, not during a prod incident review.
— And how do you even know so much about Docker?
— Three years of operating containers in production: building images, orchestration,
analyzing failures from logs and metrics."""

# Strict: demanding, not irritated, and without any swearing; that is what sets it apart
# from "rude". An examiner, not a tired colleague: it does not tolerate vague wording and
# names an inaccuracy at once, but keeps this within a professional tone. It is for when an
# honest assessment of an answer is needed, without softening, and not just sharpness for
# sharpness's sake.
TONE_STRICT = """The tone is switched to strict: you answer like a demanding examiner: precisely,
with no leniency and no comforting softening, but without rudeness and without swearing.
This is about strictness, not anger.

No "Sure!", "Good question!" and no polite introductions: straight to the point.
No hedging ("possibly", "probably", "in general"): if you know the answer, say it
directly; if the question or the solution on the screen contains an inaccuracy, name it
at once, not in passing.
No tolerance for vague wording: if the person is not sure what they are asking, first
clarify what exactly is needed, and only then answer.
No swearing and no over-familiarity: strictness rests on precision, not on tone.

— How's that bug, did you figure it out?
— Figured it out. The cause is an extra space in the config, and it cost you two hours
that a config-parsing test would have removed in a second.
— Maybe we should add tests?
— Not "maybe": we should, and it should have been done before the config went to prod.
— And how do you even know so much about Docker?
— Three years of operating it in production. If you need specific experience, ask for
specifics, not "how do you know"."""

# Trolling: the jabs are aimed at the person asking, not at the world in general. That is
# what sets it apart from "rude", where the swearing goes into the void with no addressee.
# It is the riskiest of the tones: without an explicit rule "after the jab, an exact
# answer", the model easily slides into a jab instead of an answer rather than alongside
# one, hence a separate, insistent line on exactly that.
TONE_TROLLING = """The tone is switched to trolling: you rag on the person who is asking. Sarcasm
and jabs aimed straight at them, not general grumbling into the void, but the correct
answer is always in the jab, it is not instead of it.

No "Sure!", "Good question!": instead, a comment on the question itself or on the fact
that it is asked this way.
Jab at the interlocutor directly ("seriously, is that the whole question?", "well, you
really delivered"), not at the topic of the question by itself: the aim is to needle the
person, not to complain about life.
Swearing is allowed, but it is not the main weapon: the main thing here is irony and
jabbing, swearing is just salt on top.
After the jab you always get to a real, exact answer: the ribbing is not a replacement
for usefulness but mixed in with it.

— How's that bug, did you figure it out?
— Of course. Two hours on an extra space in the config, and you would not have even
noticed it, right? That is exactly what it was: an extra space, the parser reads it as a
new nesting level.
— Maybe we should add tests?
— Genius, and it never occurred to you before? Yes, we should: a test for indentation
edge cases in the config, and we never talk about this again.
— And how do you even know so much about Docker?
— The way people usually know: three years on prod, not from a weekend YouTube course,
like some people."""

# Seven modes: the code, the label in the admin page, and the text for the prompt, as
# triples and in one place, on the same principle as LANGUAGES below. "business" is empty
# not because a business tone is the absence of a tone, but because it is what the prompt
# contained before this field existed: the section works for interviews and for study
# alike, and swearing and "so, listen" cannot be turned on by default, but they can be
# turned off for a particular conversation in one move, right here.
#
# The order in the list runs from the most restrained to the loosest, and that is also the
# order in the admin dropdown: the person sees a ladder, not names in no order.
TONES = [
    ("business", "business", ""),
    ("technical", "technical", TONE_TECHNICAL),
    ("neutral", "standard", TONE_NEUTRAL),
    ("friendly", "friendly", TONE_FRIENDLY),
    ("strict", "strict", TONE_STRICT),
    ("rude", "rude", TONE_RUDE),
    ("trolling", "trolling", TONE_TROLLING),
]

TONE_OPTIONS = [(code, label) for code, label, _ in TONES]
TONE_RULES = {code: rule for code, _, rule in TONES}


# The header of the lectures section. It sits inside the block, not in the general role,
# and that matters: without materials there is no section, but the rule "answer from the
# materials" would stay in the prompt, and the model would start looking for something it
# was not given.
#
# A separate line about "this is not in the materials" is here because silence about it is
# the most expensive of all possible answers. A generally correct answer, said in the
# wrong words and the wrong order, is heard by the teacher as alien; and the person at the
# screen does not even know they are answering off the course.
MATERIALS_RULE = (
    "Below is what the conversation is about: lectures, notes, a study guide.\n"
    "Answer from them, in their words and in their order. If the answer is not in them,\n"
    "say so in the first line and only then answer on your own."
)

# Answer length. It switches in the middle of a conversation in one move, because the HR
# part and the algorithm section need different things: a line is read by eye between
# turns, while code is carried into an editor whole. The answer has no separate "form":
# "briefly" is already stated in the core rules, and it is not repeated here. `line` is
# the usual conversational mode and has nothing to add; a line goes into the prompt only
# for what is truly different.
FORMATS = {
    "long": "Three to five points, each on a new line.",
    "code": "Code first, then one line of explanation. Take the problem statement "
            "from the screen literally.",
}


# Answer language: the code, the label in the admin page, the line for the prompt. As
# triples and in one place: a list written twice drifts apart on the first edit, and there
# would be no way to tell which of the two versions is in effect.
#
# Empty by default, and that is not "not chosen" but a working value: "as in the
# conversation" is more accurate than a recorded language, since people switch to English
# in the middle of a phrase, and an explicit choice would only get in the way here. The
# whole rule lives here, in the channel settings, and not in the core prompt: the answer
# language is decided by the channel, not by the engine.
#
# This same list is the interface and speech recognition languages (`ui/admin.py`), and
# for the interface each one has a dictionary in `ui/static/i18n/<code>.json`. A new
# language is added here, in `ui/i18n.py` (SUPPORTED), and as a dictionary; the test
# `tests/test_i18n.py` checks that the three places do not drift apart.
LANGUAGES = [
    ("", "same as conversation", "Answer in the language of the conversation."),
    ("ru", "русский", "Всегда отвечай по-русски, даже если разговор идёт на другом языке."),
    ("uk", "українська", "Завжди відповідай українською, навіть якщо розмова йде іншою мовою."),
    ("en", "English", "Always answer in English, even if the conversation is in another language."),
    ("es", "Español", "Responde siempre en español, aunque la conversación sea en otro idioma."),
    ("pt", "Português (Brasil)", "Responda sempre em português do Brasil, mesmo que a conversa seja em outro idioma."),
    ("de", "Deutsch", "Antworte immer auf Deutsch, auch wenn das Gespräch in einer anderen Sprache geführt wird."),
    ("fr", "Français", "Réponds toujours en français, même si la conversation se déroule dans une autre langue."),
    ("tr", "Türkçe", "Konuşma başka bir dilde olsa bile her zaman Türkçe yanıt ver."),
    ("pl", "Polski", "Zawsze odpowiadaj po polsku, nawet jeśli rozmowa toczy się w innym języku."),
]

LANGUAGE_OPTIONS = [(code, label) for code, label, _ in LANGUAGES]
LANGUAGE_RULES = {code: rule for code, _, rule in LANGUAGES}


def compose(settings: AssistSettings, materials: str = "", has_image: bool = False) -> str:
    """Build the system prompt: role, profile, vacancy, materials, rules.

    The materials arrive as ready text, not as a folder path, and that is deliberate: prompt
    assembly stays a pure function of the settings, visible in a test as a whole. Reading
    the files is done by `install()`, where the project root is known.

    `has_image` builds the prompt variant for a request with an attached screen frame, or
    without one. `install()` calls `compose` twice and keeps both variants ready: which of
    them goes into a given question is decided later, in `Assistant._ask`, by the fact of
    whether that question has a frame.
    """
    from argus.brain.assistant import default_system

    base = settings.prompting.system_base.strip() or default_system(has_image).strip()
    parts = [base, ROLE]

    tone = TONE_RULES.get(settings.prompting.tone, "")
    if tone:
        parts.append(tone)

    profile = _profile_block(settings)
    if profile:
        parts.append(profile)

    vacancy = _vacancy_block(settings)
    if vacancy:
        parts.append(vacancy)

    stop = _stop_block(settings)
    if stop:
        parts.append(stop)

    # The materials go after everything short and before the answer rules. This is the longest
    # piece of the prompt, and a rule placed before it would be read across thirty thousand
    # tokens of lecture: the last thing the model sees before the question should say how to
    # answer, not what to read.
    if materials.strip():
        parts.append(f"# Materials\n{MATERIALS_RULE}\n\n{materials.strip()}")

    # The "long" / "code" format gets its own line, without a heading: it is not a separate
    # topic but a short correction to what the core rules already say. The ordinary
    # conversational format has nothing to add: `FORMATS` is silent about it, and nothing
    # appears here.
    format_hint = FORMATS.get(settings.answering.format, "")
    if format_hint:
        parts.append(format_hint)

    # Language gets its own line, on the same principle: by default it sounds "as in the
    # conversation", and an explicit choice overrides that. Why empty by default is a working
    # value and not "not chosen": see LANGUAGES.
    language = LANGUAGE_RULES.get(settings.profile.language, "")
    if language:
        parts.append(language)

    return "\n\n".join(parts)


def _profile_block(s: AssistSettings) -> str:
    p = s.profile
    lines = []
    if p.name:
        lines.append(f"Name: {p.name}")
    if p.role:
        lines.append(f"Role: {p.role}" + (f", {p.years} years of experience" if p.years else ""))
    elif p.years:
        lines.append(f"Experience: {p.years} years")
    if p.stack:
        lines.append(f"Stack: {p.stack}")
    if p.summary:
        # No label of its own: the section is already called "Context", and the same
        # word again over the text is noise. Not a résumé: there is no formal résumé here, it is
        # free text about the person or about the conversation, which is what the field is
        # actually filled with.
        lines.append(f"\n{p.summary.strip()}")
    if p.projects:
        lines.append(f"\nProjects:\n{p.projects.strip()}")
    if p.story:
        # Under a separate heading, because these are answers, not facts: the model should retell
        # them in its own words, not quote them.
        lines.append(f"\nAbout me and the transition (use as the basis of the answer):\n{p.story.strip()}")
    return "# Context\n" + "\n".join(lines) if lines else ""


def _stop_block(s: AssistSettings) -> str:
    """Prohibitions as a separate section, not a line in the description of experience.

    A prohibition dissolved in text is read by the model as a wish. A separate heading is
    the only form it obeys reliably, and the cost of a mistake here is lopsided: a person
    having to defend an invented project out loud.
    """
    text = s.profile.stop_topics.strip()
    return f"# What not to do\n{text}" if text else ""


def _vacancy_block(s: AssistSettings) -> str:
    """The "About this conversation" section: what is going on and in what
    words to answer.

    The section appears only if the person wrote something. An empty heading with just the
    kind of conversation inside is a line paid for in tokens on every request that adds not
    one fact for the model.
    """
    v = s.vacancy
    if not (v.company or v.position or v.description or v.stage):
        return ""

    lines = []
    if v.stage:
        lines.append(f"Kind of conversation: {v.stage}.")
    if v.company or v.position:
        lines.append(" · ".join(x for x in (v.company, v.position) if x))
    if v.description:
        lines.append(f"\nContext:\n{v.description.strip()}")
    return "# About this conversation\n" + "\n".join(lines) if lines else ""


def install(cfg, app=None) -> str:
    """Build the prompt from the settings and put it into the live config.

    Called at startup and after every profile edit. `cfg.brain` is the same object the
    assistant holds, so the edit reaches the next request without rebuilding the pipeline.

    The materials are read and the cache is warmed here too. The moment is not accidental:
    this is the only place where the prompt becomes final. Warming earlier, in
    `Assistant.start()`, would mean writing the core's prompt cache, which is replaced by
    this one right away; warming later is nobody's job.
    """
    from argus.assist.materials import Materials, describe, load
    from argus.assist.settings import read

    settings = read(cfg)
    m = settings.materials
    loaded = load(cfg, m.max_chars, m.max_pages) if m.enabled else Materials()
    if loaded.items:
        # A line in the log, because a lecture that silently went into the prompt and one that
        # silently did not look the same, that is, like nothing.
        log.info("materials: %s", describe(loaded))

    cfg.brain.system_prompt = compose(settings, loaded.text)
    cfg.brain.system_prompt_with_image = compose(settings, loaded.text, has_image=True)
    # The frame goes to the model if the setting allows it **and** the person has not cleared
    # the "Screen" checkbox on the control panel: it lives in `app.sources` and is not
    # saved anywhere. Before, there was a single setting here, and any edit of the channel (and
    # now opening a chat) turned the frames back on while the checkbox was off.
    screen_on = getattr(app, "sources", {}).get("screen", True)
    cfg.brain.send_screenshot = settings.prompting.send_screen and screen_on

    if app is not None and app.assistant is not None:
        # The pages as images live on the assistant, not in the config: they are bytes, and they
        # cannot go into the settings file. We assign the whole value instead of appending: the
        # materials are what is in the folder **now**.
        app.assistant.pinned = loaded.images
        app.assistant.warm_soon()
    return cfg.brain.system_prompt
