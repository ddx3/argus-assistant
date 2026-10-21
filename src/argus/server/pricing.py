"""What we sell and at what price: money on the balance, not a subscription.

## How it works

A person puts an amount on the account (five dollars, twenty, whatever they want) and
spends it on everything the program can do: speech recognition, model answers, speech
synthesis. There are no subscriptions, no plans, and no "hours per month". Each call to a
paid service costs us a certain amount, and we take the same from the balance multiplied
by `MARKUP`. The balance does not expire.

One way to pay for everything is the main point of this design. The program turns from an
"interview assistant" into a universal one: a new feature does not need a new plan, it
just gets a line in this file.

## Markup: x2

`MARKUP = 2.0`: the person pays twice what the provider bills us. Not "like everyone else
on the market" but for three reasons of our own:

- **The payment provider takes its cut.** Typical card acquiring is about 3% and 30 cents
  per payment. On a five-dollar top-up that is 9%, and without a markup we would run at a
  loss on the smallest payments (see `test_the_smallest_top_up_stays_profitable`).
- **Provider prices move, and our estimates are imprecise.** Speech synthesis and
  recognition are calculated from the price list and an estimate, not from a real monthly
  bill. A twofold margin absorbs an error of one and a half times without going into loss.
- **The remainder on the balance is also ours.** Someone who topped up $20 and spent $12
  left us the unused part. We do not "burn" it, it stays with the person, but in practice
  some people do not come back: this is not planned revenue but a cushion that does not
  need to be built into the price.

It is changed with one number, right here. The price must not be touched without
recalculating `test_the_smallest_top_up_stays_profitable`.

## The unit is a micro-dollar

Balance and usage are stored as whole micro-dollars (`MICRO` per dollar). A model answer
costs a fraction of a cent, and floating-point cents drift apart over a thousand calls;
integers do not. The person is shown dollars and cents.

## Sign-up gift

`WELCOME_MICRO` is thirty cents. There is no protection against receiving it again (a new
email means a new gift, see `accounts.py`); there are only daily caps per server and per
address (`accounts.GIFT_DAILY_CAP`). So the gift is one we do not mind giving to the same
person several times: at an answer price of half a cent, it covers a few dozen answers and
half an hour of recognition.

## Where the numbers come from

Model prices are the providers' price list of 2026-10-06, per million tokens. Models that
are not listed here are not sold by the relay at all: otherwise the client would ask for
the most expensive one available to us, and we would learn the price from the bill.

Recognition is an **estimate**, about $0.30 per hour of audio: `gpt-4o-mini-transcribe`
costs $0.18 per hour, and the rest is a margin for the overhead of Realtime sessions. The
"More accurate" tier (the full `gpt-4o-transcribe`, $0.36 per hour) is $0.48 by
the same arithmetic. Speech synthesis is an **estimate** of $0.12 per 1000 characters, a
typical ElevenLabs price on volume plans. Both should be checked against a real bill after
the first live month and corrected here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MICRO = 1_000_000              # micro-dollars in a dollar
CENT = MICRO // 100

CURRENCY = "USD"
SYMBOL = "$"

MARKUP = 2.0

WELCOME_MICRO = 300_000        # $0.30 at registration

# A top-up is whole dollars. Cents on a button give nothing, and tiny payments are eaten by
# the payment provider's fees: the lower bound is not politeness but arithmetic.
TOPUP_PRESETS = (5, 10, 20, 50)   # buttons in the app
MIN_TOPUP = 5
MAX_TOPUP = 500

# Below this the balance is shown as "running low".
LOW_BALANCE_MICRO = 500_000    # $0.50

# Cost price is what we pay. The person pays `charged(...)` of it.
STT_COST_PER_HOUR = 0.30       # $ per hour of audio, an estimate (see the docstring)
# "More accurate" is the full recognition model instead of the lightweight one: its
# price per audio is twice as high ($0.36 against $0.18 per hour), and the overhead is the
# same as for the normal one. Also an estimate; check against the first real bill.
STT_COST_PER_HOUR_HIGH = 0.48
STT_TIERS = {"normal": STT_COST_PER_HOUR, "high": STT_COST_PER_HOUR_HIGH}
TTS_COST_PER_1000_CHARS = 0.12  # $ per 1000 characters, an estimate (see the docstring)

# For the "is there enough money" estimate before a request: how many tokens per character,
# and what an image costs. Pessimistic: underestimating means giving an answer that we later
# cannot charge for.
CHARS_PER_TOKEN = 2.0
TOKENS_PER_IMAGE = 2_000

# A typical answer, for the "≈ $0.003 per answer" line in the app. The
# conversation feed, the profile, and a screen frame on input, a short answer on output.
TYPICAL_ANSWER_IN = 3_000
TYPICAL_ANSWER_OUT = 300


class UnknownModel(Exception):
    """The model is not in the price list: the relay does not sell it."""


@dataclass(frozen=True)
class ModelPrice:
    """The model's price per million tokens, in dollars."""

    provider: str   # openai-api | anthropic-api, as in the relay request
    title: str
    input: float
    output: float


MODELS: dict[str, ModelPrice] = {
    # OpenAI. Luna is the normal tier, Terra is "Think longer", Sol is the top
    # one.
    "gpt-5.6-luna": ModelPrice("openai-api", "GPT-5.6 Luna", 0.20, 1.20),
    "gpt-5.6-terra": ModelPrice("openai-api", "GPT-5.6 Terra", 2.00, 12.00),
    "gpt-5.6-sol": ModelPrice("openai-api", "GPT-5.6 Sol", 5.00, 30.00),
    # Anthropic.
    "claude-haiku-4-5": ModelPrice("anthropic-api", "Claude Haiku 4.5", 1.00, 5.00),
    "claude-sonnet-5-5": ModelPrice("anthropic-api", "Claude Sonnet 5.5", 2.00, 10.00),
    "claude-opus-5-5": ModelPrice("anthropic-api", "Claude Opus 5.5", 4.00, 20.00),
}


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------


def usd(micro: int | float) -> float:
    return micro / MICRO


def micro_of(dollars: float) -> int:
    return round(dollars * MICRO)


def charged(cost_micro: int | float) -> int:
    """How much to charge the balance for usage that costs us `cost_micro`.

    We round up: a shortfall of a fraction of a micro-dollar accumulates into a loss, while
    an excess does not, since nobody will notice it.
    """
    return math.ceil(cost_micro * MARKUP)


def money(micro: int | float) -> str:
    """For a human: `$4.87`, and small amounts with three digits, `$0.006`.

    An answer price of half a cent shown as `$0.01` would overstate it twofold.
    """
    value = usd(micro)
    if value == 0:
        return f"{SYMBOL}0"
    if abs(value) < 0.1:
        return f"{SYMBOL}{value:.3f}"
    return f"{SYMBOL}{value:.2f}"


# --------------------------------------------------------------------------
# Cost price by kind of usage
# --------------------------------------------------------------------------


def stt_cost(seconds: float, tier: str = "normal") -> int:
    return math.ceil(seconds * STT_TIERS[tier] / 3600 * MICRO)


def tts_cost(chars: int) -> int:
    return math.ceil(chars / 1000 * TTS_COST_PER_1000_CHARS * MICRO)


def price_of(model: str) -> ModelPrice:
    try:
        return MODELS[model]
    except KeyError:
        raise UnknownModel(model) from None


def brain_cost(model: str, input_tokens: int, output_tokens: int) -> int:
    p = price_of(model)
    return math.ceil((input_tokens * p.input + output_tokens * p.output))


def brain_ceiling(model: str, text_chars: int, images: int, max_tokens: int,
                  text_bytes: int = 0) -> int:
    """Ceiling of a request's cost, before it goes to the model.

    The input is estimated pessimistically from the text length and the number of images, and
    the output as the full `max_tokens` (for models with reasoning, its count includes the
    reasoning, so the upper bound is honest).

    `text_bytes` is the size of the same text in UTF-8 bytes. Outside Latin script a character
    can cost a token or more: emoji, rare ideographs, and many scripts give two to four tokens
    per character, and an estimate of "two characters per token" would understate their input
    several times over. Such a character also takes two to four bytes, so we take the larger
    of the two numbers. For Latin text it is the same thing; for the rest the ceiling grows by
    as much as a character is longer than a byte, that is, "two bytes per token" instead of
    "two characters". Even at one token per byte this would not be an absolute bound (a token
    is no shorter than a byte). We took the same ratio as for Latin text so that the ceiling
    does not scare off honest requests when only a few cents are left.
    """
    units = max(text_chars, text_bytes)
    tokens_in = math.ceil(units / CHARS_PER_TOKEN) + images * TOKENS_PER_IMAGE
    return brain_cost(model, tokens_in, max_tokens)


# --------------------------------------------------------------------------
# Price list for the person
# --------------------------------------------------------------------------


# Answer showcase: the tiers the person sees, instead of model names. Which models we have
# under the hood is our own business, and it does not go into the app, neither in the
# pricing nor in the server's answers (`test_the_sheet_keeps_the_models_secret`). The price
# of a tier is calculated from the model named here, and that is what we charge by.
ANSWER_TIERS = (
    ("Regular answer", "gpt-5.6-luna"),
    ("“Think longer”", "gpt-5.6-terra"),
)


def price_sheet() -> dict:
    """What the app shows in the "Pricing" section. The markup is already inside, and
    it is not given out itself: the person cares about the price, not what it is made of.
    There are no models here, only answer tiers (`ANSWER_TIERS`).
    """
    answers = []
    for title, model_id in ANSWER_TIERS:
        typical = charged(brain_cost(model_id, TYPICAL_ANSWER_IN, TYPICAL_ANSWER_OUT))
        answers.append({"title": title, "answer": money(typical), "answer_micro": typical})
    stt_hour = charged(stt_cost(3600))
    stt_hour_high = charged(stt_cost(3600, "high"))
    return {
        "currency": CURRENCY,
        # As a string to display, as a number to calculate ("enough for so many hours": the app
        # divides the remainder by these numbers and does not parse a string with "$").
        "stt_hour": money(stt_hour),
        "stt_hour_micro": stt_hour,
        "stt_minute": money(charged(stt_cost(60))),
        # "More accurate" is the same hour of speech, but a stronger model listens. An old
        # app does not read these keys and loses nothing.
        "stt_hour_high": money(stt_hour_high),
        "stt_hour_high_micro": stt_hour_high,
        "stt_minute_high": money(charged(stt_cost(60, "high"))),
        "tts_1000": money(charged(tts_cost(1000))),
        "tts_1000_micro": charged(tts_cost(1000)),
        "answers": answers,
        "topups": list(TOPUP_PRESETS),
        "min_topup": MIN_TOPUP,
        "max_topup": MAX_TOPUP,
        "welcome": money(WELCOME_MICRO),
    }
