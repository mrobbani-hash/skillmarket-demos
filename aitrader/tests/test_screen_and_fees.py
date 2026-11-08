from decimal import Decimal

import pytest

from paper.fees import QuoteRejected, estimate_liquidation, fit_entry
from paper.limits import INITIAL_SIM_NOW, NET_TARGET, PER_TRADE_CAP
from paper.market import SyntheticMarket
from paper.presets import example_preset, validate_policy
from paper.screen import screen_snapshot


def policy():
    return validate_policy(example_preset())


def base_snap(**changes):
    market = SyntheticMarket()
    snap = market.snapshot("SYN-ALPHA", INITIAL_SIM_NOW)
    snap.update(changes)
    return snap


def test_example_preset_is_labeled_and_not_a_wallet():
    raw = example_preset()
    assert raw["illustrative"] is True
    assert raw["session_budget_sol"] != "1.18"
    stored = policy()
    assert stored["fee_model"]["disclaimer"]
    assert "not an executable GMGN quote" in stored["fee_model"]["disclaimer"]


def test_missing_fee_component_and_over_cap_are_rejected():
    model = dict(example_preset()["fee_model"])
    model.pop("tip_fee_sol")
    with pytest.raises(QuoteRejected) as exc:
        fit_entry(model)
    assert exc.value.code == "unknown_cost"
    model = dict(example_preset()["fee_model"])
    model["priority_fee_sol"] = "0.05"
    model["tip_fee_sol"] = "0.05"
    with pytest.raises(QuoteRejected) as exc:
        fit_entry(model)
    assert exc.value.code == "nonpositive_executable_size"
    model = dict(example_preset()["fee_model"])
    del model["ata_reclaimed_on_full_sell"]
    with pytest.raises(QuoteRejected):
        fit_entry(model)


def test_envelope_includes_sell_reserve_and_stays_inside_cap():
    quote = fit_entry(example_preset()["fee_model"])
    committed = Decimal(quote["committed"])
    buy = Decimal(quote["buy_input"])
    parts = sum(Decimal(quote[key]) for key in (
        "buy_input", "buy_percent_fee", "buy_fixed_fee", "ata_rent", "sell_reserve",
    ))
    assert buy > 0
    assert Decimal(quote["sell_reserve"]) > Decimal(quote["sell_fixed_fee"])
    assert parts == committed
    assert committed <= PER_TRADE_CAP
    assert committed > Decimal("0.03")
    assert quote["ata_reclaimed_on_full_sell"] is False
    assert quote["label"] == "ILLUSTRATIVE_NOT_A_GMGN_QUOTE"


def _position_from_quote(quote, price="0.00001"):
    from paper.fees import filled_units
    return {
        "filled_units": filled_units(quote["buy_input"], price, quote["buy_slippage_rate"]),
        "entry_price_sol": price,
        "gmgn_fee_rate": quote["gmgn_fee_rate"],
        "platform_fee_rate": quote["platform_fee_rate"],
        "sell_slippage_rate": quote["sell_slippage_rate"],
        "sell_reserve": quote["sell_reserve"],
        "sell_fixed_fee": quote["sell_fixed_fee"],
        "committed": quote["committed"],
        "ata_rent": quote["ata_rent"],
        "ata_reclaimed_on_full_sell": False,
    }


def test_twenty_percent_gross_is_below_net_target_and_a_higher_price_crosses_it():
    quote = fit_entry(example_preset()["fee_model"])
    pos = _position_from_quote(quote)
    now = 1_000
    gross = estimate_liquidation(pos, "0.000012", now, now, 120)
    assert Decimal(gross["gross_ratio"]) == Decimal("0.2")
    assert gross["actionable"] is True
    assert Decimal(gross["net_ratio"]) < NET_TARGET
    higher = estimate_liquidation(pos, "0.000018", now, now, 120)
    assert Decimal(higher["gross_ratio"]) == Decimal("0.8")
    assert Decimal(higher["net_ratio"]) >= NET_TARGET


def test_stale_or_unknown_quote_has_no_actionable_net():
    quote = fit_entry(example_preset()["fee_model"])
    pos = _position_from_quote(quote)
    stale = estimate_liquidation(pos, "0.00003", 50, 1_000, 120)
    assert stale["fresh"] is False
    assert stale["net_ratio"] is None
    assert stale["actionable"] is False
    unknown = estimate_liquidation(pos, None, 1_000, 1_000, 120)
    assert unknown["reason"] == "unknown_price"
    assert unknown["net"] is None


def test_launch_age_uses_creation_time_only():
    pol = policy()
    now = INITIAL_SIM_NOW
    assert screen_snapshot(base_snap(), pol, now)["eligible"] is True
    old = screen_snapshot(base_snap(creation_timestamp=now - 7200, open_timestamp=now - 30), pol, now)
    assert old["eligible"] is False
    assert any(item["name"] == "launch_age" and item["status"] == "fail" for item in old["checks"])
    pool = base_snap()
    pool["creation_timestamp"] = None
    pool["open_timestamp"] = now - 60
    decision = screen_snapshot(pool, pol, now)
    assert decision["eligible"] is False
    assert decision["ignored_open_timestamp"] is True
    assert any(item["name"] == "launch_age" and item["status"] == "unknown" for item in decision["checks"])
    edge = screen_snapshot(base_snap(creation_timestamp=now - 3600), pol, now)
    assert edge["eligible"] is False
    just = screen_snapshot(base_snap(creation_timestamp=now - 3599), pol, now)
    assert just["eligible"] is True


def test_missing_safety_fields_do_not_default_safe():
    pol = policy()
    now = INITIAL_SIM_NOW
    for field, expected in (
        ("honeypot", "unknown"),
        ("renounced_mint", "unknown"),
        ("renounced_freeze", "unknown"),
        ("is_wash_trading", "unknown"),
        ("top10_rate", "unknown"),
        ("dev_hold_rate", "unknown"),
        ("bundler_rate", "unknown"),
        ("liquidity_usd", "unknown"),
        ("lp_withdrawal_count", "unknown"),
        ("token2022_transfer_fee_bps", "unknown"),
        ("token2022_permanent_delegate", "unknown"),
    ):
        snap = base_snap()
        snap[field] = None
        decision = screen_snapshot(snap, pol, now)
        assert decision["eligible"] is False
        assert decision["safety_certification"] is False
        assert any(item["status"] == expected for item in decision["checks"])
    unsupported = screen_snapshot(base_snap(lp_withdrawal_count=0), pol, now, unsupported={"lp_withdrawal_count"})
    assert unsupported["eligible"] is False
    assert any(item["name"] == "lp_withdrawals" and item["status"] == "unsupported" for item in unsupported["checks"])
    refused = screen_snapshot(base_snap(synthetic=False), pol, now)
    assert refused["eligible"] is False


def test_explicit_bad_flags_fail():
    pol = policy()
    now = INITIAL_SIM_NOW
    assert screen_snapshot(base_snap(honeypot=True), pol, now)["eligible"] is False
    assert screen_snapshot(base_snap(renounced_freeze=False), pol, now)["eligible"] is False
    assert screen_snapshot(base_snap(is_wash_trading=True), pol, now)["eligible"] is False
    assert screen_snapshot(base_snap(lp_withdrawal_count=2), pol, now)["eligible"] is False
    assert screen_snapshot(base_snap(token2022_permanent_delegate=True), pol, now)["eligible"] is False
    no_policy = screen_snapshot(base_snap(), None, now)
    assert no_policy["eligible"] is False
    assert any(item["status"] == "unknown" for item in no_policy["checks"])
