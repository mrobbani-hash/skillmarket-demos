"""Illustrative all-in sizing and net liquidation math.

Nothing here calls a venue. Numbers exist only after the caller supplies every
cost. A missing cost is a rejection, not a zero.
"""

from decimal import Decimal

from paper.limits import MODEL_LABEL, NOT_A_QUOTE, PER_TRADE_CAP
from paper.money import D, lamport_down, lamport_up


class QuoteRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _rate(raw, name: str) -> Decimal:
    if isinstance(raw, bool) or raw is None:
        raise QuoteRejected("unknown_cost", f"{name} is missing")
    try:
        value = D(raw)
    except Exception as exc:
        raise QuoteRejected("unknown_cost", f"{name} is not a number") from exc
    if value < 0 or value >= 1:
        raise QuoteRejected("invalid_fee_model", f"{name} must be in [0, 1)")
    return value


def _sol(raw, name: str) -> Decimal:
    if isinstance(raw, bool) or raw is None:
        raise QuoteRejected("unknown_cost", f"{name} is missing")
    try:
        value = D(raw)
    except Exception as exc:
        raise QuoteRejected("unknown_cost", f"{name} is not a number") from exc
    if value < 0:
        raise QuoteRejected("invalid_fee_model", f"{name} must be >= 0")
    return value


def normalize_fee_model(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise QuoteRejected("unknown_cost", "fee model is missing")
    required = (
        "gmgn_fee_rate",
        "platform_fee_rate",
        "priority_fee_sol",
        "tip_fee_sol",
        "base_fee_sol",
        "ata_rent_sol",
        "buy_slippage_rate",
        "sell_slippage_rate",
        "ata_reclaimed_on_full_sell",
    )
    missing = [key for key in required if key not in raw]
    if missing:
        raise QuoteRejected("unknown_cost", "fee model missing: " + ", ".join(missing))
    extra = set(raw) - set(required)
    if extra:
        raise QuoteRejected("invalid_fee_model", "unknown fee fields: " + ", ".join(sorted(extra)))
    reclaimed = raw["ata_reclaimed_on_full_sell"]
    if not isinstance(reclaimed, bool):
        raise QuoteRejected("unknown_cost", "ata reclaim assumption must be an explicit true or false")
    model = {
        "label": MODEL_LABEL,
        "disclaimer": NOT_A_QUOTE,
        "gmgn_fee_rate": _rate(raw["gmgn_fee_rate"], "gmgn_fee_rate"),
        "platform_fee_rate": _rate(raw["platform_fee_rate"], "platform_fee_rate"),
        "priority_fee_sol": _sol(raw["priority_fee_sol"], "priority_fee_sol"),
        "tip_fee_sol": _sol(raw["tip_fee_sol"], "tip_fee_sol"),
        "base_fee_sol": _sol(raw["base_fee_sol"], "base_fee_sol"),
        "ata_rent_sol": _sol(raw["ata_rent_sol"], "ata_rent_sol"),
        "buy_slippage_rate": _rate(raw["buy_slippage_rate"], "buy_slippage_rate"),
        "sell_slippage_rate": _rate(raw["sell_slippage_rate"], "sell_slippage_rate"),
        "ata_reclaimed_on_full_sell": reclaimed,
    }
    percent = model["gmgn_fee_rate"] + model["platform_fee_rate"]
    if percent >= 1:
        raise QuoteRejected("invalid_fee_model", "combined percent fees must be < 100%")
    return model


def fit_entry(model: dict, cap: Decimal = PER_TRADE_CAP) -> dict:
    """Size the buy input so buy input + modeled fees + sell reserve == cap, or reject."""
    raw = dict(model or {})
    raw.pop("label", None)
    raw.pop("disclaimer", None)
    model = normalize_fee_model(raw)
    cap = D(cap)
    if cap <= 0 or cap > PER_TRADE_CAP:
        raise QuoteRejected("over_cap", "per-trade envelope cannot exceed 0.04 paper SOL")
    f = model["gmgn_fee_rate"] + model["platform_fee_rate"]
    buy_slip = model["buy_slippage_rate"]
    fixed_side = model["priority_fee_sol"] + model["tip_fee_sol"] + model["base_fee_sol"]
    ata = model["ata_rent_sol"]
    # Sell percent reserve is estimated on the post-buy-slippage entry value so a
    # flat exit can still pay that modeled percent fee from locked cash.
    coef = Decimal(1) + f + (Decimal(1) - buy_slip) * f
    fixed = fixed_side + fixed_side + ata
    if coef <= 0 or fixed >= cap:
        raise QuoteRejected("nonpositive_executable_size", "modeled fixed costs leave no buy input inside 0.04")
    buy_input = lamport_down((cap - fixed) / coef)

    def parts(amount: Decimal):
        buy_percent = lamport_up(amount * f)
        sell_percent_reserve = lamport_up(amount * (Decimal(1) - buy_slip) * f)
        sell_reserve = fixed_side + sell_percent_reserve
        committed = amount + buy_percent + fixed_side + ata + sell_reserve
        return buy_percent, sell_percent_reserve, sell_reserve, committed

    buy_percent, sell_percent_reserve, sell_reserve, committed = parts(buy_input)
    while buy_input > 0 and committed > cap:
        buy_input -= D("0.000000001")
        buy_percent, sell_percent_reserve, sell_reserve, committed = parts(buy_input)
    if buy_input <= 0 or committed > cap or committed <= 0:
        raise QuoteRejected("nonpositive_executable_size", "no positive buy input fits the 0.04 envelope")
    return {
        "label": MODEL_LABEL,
        "disclaimer": NOT_A_QUOTE,
        "cap": format(cap, "f"),
        "buy_input": format(buy_input, "f"),
        "buy_percent_fee": format(buy_percent, "f"),
        "buy_fixed_fee": format(fixed_side, "f"),
        "ata_rent": format(ata, "f"),
        "sell_fixed_fee": format(fixed_side, "f"),
        "sell_percent_reserve": format(sell_percent_reserve, "f"),
        "sell_reserve": format(sell_reserve, "f"),
        "committed": format(committed, "f"),
        "gmgn_fee_rate": format(model["gmgn_fee_rate"], "f"),
        "platform_fee_rate": format(model["platform_fee_rate"], "f"),
        "buy_slippage_rate": format(buy_slip, "f"),
        "sell_slippage_rate": format(model["sell_slippage_rate"], "f"),
        "priority_fee_sol": format(model["priority_fee_sol"], "f"),
        "tip_fee_sol": format(model["tip_fee_sol"], "f"),
        "base_fee_sol": format(model["base_fee_sol"], "f"),
        "ata_reclaimed_on_full_sell": model["ata_reclaimed_on_full_sell"],
    }


def filled_units(buy_input: str, price_sol: str, buy_slippage_rate: str) -> str:
    price = D(price_sol)
    slip = D(buy_slippage_rate)
    if price <= 0 or slip < 0 or slip >= 1:
        raise QuoteRejected("unknown_cost", "entry price or buy slippage is not usable")
    units = lamport_down((D(buy_input) / price) * (Decimal(1) - slip))
    if units <= 0:
        raise QuoteRejected("nonpositive_executable_size", "slippage and price leave zero filled units")
    return format(units, "f")


def estimate_liquidation(position: dict, price_sol, market_as_of, sim_now: int, quote_ttl: int) -> dict:
    """Net of the committed all-in, or an explicit non-actionable reason.

    Gross price change is reported only as a contrast. It is never the exit test.
    """
    base = {
        "actionable": False,
        "fresh": False,
        "available": False,
        "reason": "unknown_price",
        "net": None,
        "net_ratio": None,
        "gross_ratio": None,
        "sol_back": None,
        "disclaimer": NOT_A_QUOTE,
    }
    if price_sol is None or market_as_of is None:
        return base
    try:
        price = D(price_sol)
    except Exception:
        base["reason"] = "unknown_price"
        return base
    if price <= 0:
        base["reason"] = "unknown_price"
        return base
    try:
        age = int(sim_now) - int(market_as_of)
    except (TypeError, ValueError):
        base["reason"] = "unknown_quote_time"
        return base
    if age < 0 or age > int(quote_ttl):
        base["reason"] = "stale_quote"
        return base
    if not position.get("filled_units"):
        base["reason"] = "no_filled_units"
        base["fresh"] = True
        return base
    try:
        units = D(position["filled_units"])
        entry = D(position["entry_price_sol"])
        f = D(position["gmgn_fee_rate"]) + D(position["platform_fee_rate"])
        sell_slip = D(position["sell_slippage_rate"])
        reserve = D(position["sell_reserve"])
        fixed = D(position["sell_fixed_fee"])
        committed = D(position["committed"])
        ata = D(position["ata_rent"])
    except Exception:
        base["reason"] = "unknown_cost"
        return base
    if units <= 0 or entry <= 0 or committed <= 0 or sell_slip < 0 or sell_slip >= 1:
        base["reason"] = "unknown_cost"
        return base
    gross_ratio = (price - entry) / entry
    after_slip = units * price * (Decimal(1) - sell_slip)
    percent_fee = lamport_up(after_slip * f)
    proceeds = after_slip - percent_fee
    if reserve >= fixed:
        reserve_left = reserve - fixed
        proceeds_left = proceeds
        can_pay = proceeds_left >= 0
    else:
        shortfall = fixed - reserve
        reserve_left = Decimal(0)
        can_pay = proceeds >= shortfall
        proceeds_left = proceeds - shortfall if can_pay else Decimal(0)
    ata_back = ata if position.get("ata_reclaimed_on_full_sell") is True else Decimal(0)
    if position.get("ata_reclaimed_on_full_sell") not in (True, False):
        base["reason"] = "unknown_cost"
        base["fresh"] = True
        return base
    sol_back = proceeds_left + reserve_left + ata_back
    net = sol_back - committed
    result = {
        "actionable": bool(can_pay),
        "fresh": True,
        "available": True,
        "reason": "ok" if can_pay else "sell_costs_exceed_proceeds_and_reserve",
        "net": format(net, "f"),
        "net_ratio": format(net / committed, "f"),
        "gross_ratio": format(gross_ratio, "f"),
        "sol_back": format(sol_back, "f") if can_pay else None,
        "percent_fee": format(percent_fee, "f"),
        "disclaimer": NOT_A_QUOTE,
    }
    if not can_pay:
        result["actionable"] = False
        result["sol_back"] = None
    return result
