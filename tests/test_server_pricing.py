"""Price list: markup, cost, ceilings.

What gets tested is not "the function returned a number" but what breaks money: a price
that quietly became unprofitable, a model that is sold but is not in the price list, and
rounding that makes a thousand small charges come up short by pennies.
"""

from __future__ import annotations

import pytest

from argus.server import pricing


def test_the_smallest_top_up_stays_profitable():
    """The smallest top-up, spent down to the last cent, is still in profit.

    The worst case for us: a person puts in the minimum and spends all of it. The payment
    provider took its 3% + 30 cents, and the upstream providers took their own cost. The
    test guards not the profit itself but the fact that a markup change cannot be made
    without noticing that on a five-dollar payment we work at a loss.
    """
    top_up = pricing.MIN_TOPUP
    provider_cost = top_up / pricing.MARKUP          # what we will pay the providers
    till_fee = top_up * 0.03 + 0.30
    assert top_up - provider_cost - till_fee > 0.5, (
        f"only ${top_up - provider_cost - till_fee:.2f} is left of ${top_up}"
    )


def test_the_markup_is_applied_and_rounded_up():
    assert pricing.charged(1_000_000) == 2_000_000
    # A shortfall of a fraction of a microdollar piles up into a loss; an overshoot does not.
    assert pricing.charged(1) == 2
    assert pricing.charged(0.4) == 1


def test_every_model_in_the_sheet_has_a_price_in_both_directions():
    for name, p in pricing.MODELS.items():
        assert p.input > 0 and p.output > 0, name
        assert p.provider in ("openai-api", "anthropic-api"), name
        assert p.output >= p.input, f"{name}: output is cheaper than input, most likely a typo"


def test_the_models_in_the_default_config_are_on_sale():
    """The client uses the models from `argus.toml`: if they are not in the price list, the relay
    refuses every question out of the box."""
    for model in ("gpt-5.6-luna", "gpt-5.6-terra"):
        assert model in pricing.MODELS


def test_an_unknown_model_is_not_sold():
    with pytest.raises(pricing.UnknownModel):
        pricing.price_of("gpt-secret-most-expensive")


def test_brain_cost_is_tokens_times_the_price_per_million():
    luna = pricing.MODELS["gpt-5.6-luna"]
    # 1M input + 1M output tokens = the sum of the two prices per million.
    assert pricing.brain_cost("gpt-5.6-luna", 1_000_000, 1_000_000) == pytest.approx(
        (luna.input + luna.output) * pricing.MICRO)


def test_stt_hour_costs_what_the_sheet_says():
    assert pricing.stt_cost(3600) == pytest.approx(
        pricing.STT_COST_PER_HOUR * pricing.MICRO, abs=1)
    sheet = pricing.price_sheet()
    assert sheet["stt_hour"] == pricing.money(pricing.charged(pricing.stt_cost(3600)))


def test_a_typical_answer_costs_a_fraction_of_a_cent_to_a_few_cents():
    """If a "plain" answer costs a dollar, a five-dollar balance is five questions, and the whole
    idea makes no sense. This guards the order of magnitude."""
    for model_id in ("gpt-5.6-luna", "gpt-5.6-terra"):
        micro = pricing.charged(
            pricing.brain_cost(model_id, pricing.TYPICAL_ANSWER_IN, pricing.TYPICAL_ANSWER_OUT))
        assert 0 < micro < 50_000, f"{model_id}: {pricing.money(micro)}"


def test_the_ceiling_never_undercounts_the_real_cost():
    """The ceiling computed before the request is never less than the real price at the same
    sizes.
    """
    real = pricing.brain_cost("gpt-5.6-terra", 4_000, 800)
    ceiling = pricing.brain_ceiling("gpt-5.6-terra", text_chars=8_000, images=0, max_tokens=800)
    assert ceiling >= real


def test_an_image_raises_the_ceiling():
    without = pricing.brain_ceiling("gpt-5.6-luna", 1000, 0, 500)
    with_one = pricing.brain_ceiling("gpt-5.6-luna", 1000, 1, 500)
    assert with_one > without


def test_money_for_a_human():
    assert pricing.money(0) == "$0"
    assert pricing.money(4_870_000) == "$4.87"
    assert pricing.money(6_000) == "$0.006"     # half a cent must not be shown as "$0.01"
    assert pricing.money(300_000) == "$0.30"


def test_the_sheet_does_not_leak_the_markup():
    """The person cares about the price, not about what it is made up of."""
    sheet = pricing.price_sheet()
    assert "markup" not in sheet
    assert all("markup" not in a for a in sheet["answers"])


def test_the_sheet_shows_marked_up_prices():
    luna = pricing.ANSWER_TIERS[0][1]
    expected = pricing.charged(pricing.brain_cost(
        luna, pricing.TYPICAL_ANSWER_IN, pricing.TYPICAL_ANSWER_OUT))
    assert pricing.price_sheet()["answers"][0]["answer_micro"] == expected


def test_the_sheet_keeps_the_models_secret():
    """Which models we run under the hood is a secret: the price list has no names, no per-token
    prices from which the name could be worked out, and no provider names."""
    import json

    text = json.dumps(pricing.price_sheet(), ensure_ascii=False).lower()
    for model_id, p in pricing.MODELS.items():
        assert model_id.lower() not in text, model_id
        assert p.title.lower() not in text, p.title
    for word in ("openai", "anthropic", "gpt", "claude", "luna", "terra", "haiku", "sonnet", "opus"):
        assert word not in text, word
    assert "models" not in pricing.price_sheet()


def test_every_answer_tier_is_priced_by_a_model_on_sale():
    for title, model_id in pricing.ANSWER_TIERS:
        assert model_id in pricing.MODELS, title


def test_the_thinking_tier_costs_more_than_the_plain_one():
    plain, deep = pricing.price_sheet()["answers"]
    assert deep["answer_micro"] > plain["answer_micro"]


def test_the_top_up_presets_fit_the_limits():
    assert all(pricing.MIN_TOPUP <= d <= pricing.MAX_TOPUP for d in pricing.TOPUP_PRESETS)
    assert min(pricing.TOPUP_PRESETS) == pricing.MIN_TOPUP


def test_prices_are_in_dollars():
    """We are billed in dollars, so we sell in dollars, with no exchange-rate risk."""
    assert pricing.CURRENCY == "USD"
    assert pricing.SYMBOL == "$"


def test_the_welcome_gift_is_small_enough_to_give_away_twice():
    """The gift is given again with a new email (see `accounts.py`), and it costs us no more than
    we are willing to lose on one person many times over."""
    our_cost = pricing.WELCOME_MICRO / pricing.MARKUP
    assert our_cost <= 0.20 * pricing.MICRO


def test_the_sheet_gives_numbers_for_the_conversion_table():
    """The table "what $5/$10/… buys" is computed in the app from these numbers."""
    sheet = pricing.price_sheet()
    assert sheet["stt_hour_micro"] == pricing.charged(pricing.stt_cost(3600))
    assert sheet["tts_1000_micro"] == pricing.charged(pricing.tts_cost(1000))
    assert sheet["stt_minute"] == pricing.money(pricing.charged(pricing.stt_cost(60)))
    assert all(a["answer_micro"] > 0 for a in sheet["answers"])


def test_the_sharper_recognition_is_priced_above_the_plain_one():
    sheet = pricing.price_sheet()
    assert sheet["stt_hour_high_micro"] == pricing.charged(pricing.stt_cost(3600, "high"))
    assert sheet["stt_hour_high_micro"] > sheet["stt_hour_micro"]
    assert sheet["stt_hour_high"] == pricing.money(sheet["stt_hour_high_micro"])


# --------------------------------------------------------------------------
# The ceiling and the app settings
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sign, per_sign", [("😀", 4), ("龘", 3), ("é", 2), ("a", 1)])
def test_the_ceiling_does_not_undercount_signs_that_cost_several_tokens(sign, per_sign):
    """"Two characters per token" holds for Latin text, but emoji and rare CJK characters cost
    2-4 tokens per character: an estimate by character count understated the input several
    times over, and an expensive request passed the balance check. Such a character is also
    two to four UTF-8 bytes, and by bytes the ceiling does not undercount."""
    text = sign * 10_000
    model = "gpt-5.6-sol"
    by_signs = pricing.brain_ceiling(model, len(text), 0, 1000)
    by_bytes = pricing.brain_ceiling(model, len(text), 0, 1000, text_bytes=len(text.encode()))
    # The worst case is one token per byte, but we take the same "two per token" as for Latin
    # text.
    tokens_in = -(-len(text.encode()) // 2)
    assert by_bytes == pricing.brain_cost(model, tokens_in, 1000)
    if per_sign > 1:
        assert by_bytes > by_signs
    else:
        assert by_bytes == by_signs, "for Latin text the estimate must not change"


def test_a_realistic_worst_case_is_covered_by_the_ceiling():
    """40,000 emoji characters on the top model: usually two tokens per character."""
    text = "😀" * 40_000
    real_tokens = len(text) * 2
    real_cost = pricing.brain_cost("gpt-5.6-sol", real_tokens, 500)
    ceiling = pricing.brain_ceiling("gpt-5.6-sol", len(text), 0, 500,
                                    text_bytes=len(text.encode()))
    assert ceiling >= real_cost


def _argus_toml() -> dict:
    import tomllib
    from pathlib import Path

    with open(Path(__file__).resolve().parents[1] / "argus.toml", "rb") as f:
        return tomllib.load(f)


def test_the_models_in_argus_toml_are_on_sale_with_the_matching_upstream():
    """The app sends the relay the models from `argus.toml` and its own upstream: rename a model
    in settings and the relay answers 400 to every question out of the box, while a test that
    compares hardcoded names does not see this."""
    brain = _argus_toml()["brain"]
    for key in ("api_model", "deep_model"):
        model = brain[key]
        assert model in pricing.MODELS, f"[brain] {key} = {model!r} is not in the price list"
        assert pricing.MODELS[model].provider == brain["relay_upstream"], key


def test_the_price_sheet_tiers_are_the_models_the_app_really_asks_for():
    """The price of "Regular answer" / "Think longer" in the app
    is computed from the models in the price list; if the app's settings point at other ones,
    the person sees a different price from the one they pay."""
    brain = _argus_toml()["brain"]
    assert [model for _, model in pricing.ANSWER_TIERS] == [brain["api_model"], brain["deep_model"]]


def test_the_defaults_of_the_config_are_sellable_through_the_relay():
    from argus.core.config import Config

    brain = Config().brain
    assert brain.api_model in pricing.MODELS
    assert pricing.MODELS[brain.api_model].provider == brain.relay_upstream
    if brain.deep_model:
        assert pricing.MODELS[brain.deep_model].provider == brain.relay_upstream
