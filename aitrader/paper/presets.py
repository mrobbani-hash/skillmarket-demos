"""Policy parsing. No numeric risk preference is applied unless the caller sets it."""

from paper.fees import QuoteRejected, normalize_fee_model
from paper.limits import DISCLAIMER_ACK
from paper.money import D

POLICY_KEYS = {
    "preset_name",
    "illustrative",
    "disclaimer_ack",
    "max_positions",
    "session_budget_sol",
    "realized_loss_halt_sol",
    "per_position_loss_fraction",
    "max_hold_seconds",
    "quote_ttl_seconds",
    "screening",
    "fee_model",
}

SCREEN_KEYS = {
    "max_buy_tax",
    "max_sell_tax",
    "min_liquidity_usd",
    "max_top10",
    "max_dev_hold",
    "max_bundler",
}


class PolicyError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def example_preset() -> dict:
    """Fake-money illustration so the UI can be exercised. Not a recommendation."""
    return {
        "preset_name": "ILLUSTRATIVE_FAKE_MONEY_EXAMPLE",
        "illustrative": True,
        "disclaimer_ack": DISCLAIMER_ACK,
        "max_positions": 3,
        "session_budget_sol": "0.12",
        "realized_loss_halt_sol": "0.04",
        "per_position_loss_fraction": "0.50",
        "max_hold_seconds": 1800,
        "quote_ttl_seconds": 120,
        "screening": {
            "max_buy_tax": "0.05",
            "max_sell_tax": "0.05",
            "min_liquidity_usd": "10000",
            "max_top10": "0.30",
            "max_dev_hold": "0.10",
            "max_bundler": "0.30",
        },
        "fee_model": {
            "gmgn_fee_rate": "0.01",
            "platform_fee_rate": "0.01",
            "priority_fee_sol": "0.0001",
            "tip_fee_sol": "0.0001",
            "base_fee_sol": "0.000005",
            "ata_rent_sol": "0.00203928",
            "buy_slippage_rate": "0.01",
            "sell_slippage_rate": "0.02",
            "ata_reclaimed_on_full_sell": False,
        },
    }


def validate_policy(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise PolicyError("policy must be an object")
    extra = set(raw) - POLICY_KEYS
    missing = POLICY_KEYS - set(raw)
    if extra:
        raise PolicyError("unknown policy fields: " + ", ".join(sorted(extra)))
    if missing:
        raise PolicyError("missing policy fields: " + ", ".join(sorted(missing)))
    if raw["illustrative"] is not True:
        raise PolicyError("policy must be marked illustrative; this runtime has no real-money mode")
    if raw["disclaimer_ack"] != DISCLAIMER_ACK:
        raise PolicyError("disclaimer acknowledgement mismatch")
    if not isinstance(raw["preset_name"], str) or not raw["preset_name"].strip():
        raise PolicyError("preset_name is required")
    if not isinstance(raw["max_positions"], int) or isinstance(raw["max_positions"], bool):
        raise PolicyError("max_positions must be an integer")
    if not 1 <= raw["max_positions"] <= 20:
        raise PolicyError("max_positions must be from 1 to 20 (simulator limit, not a risk recommendation)")
    try:
        budget = D(raw["session_budget_sol"])
        halt = D(raw["realized_loss_halt_sol"])
        loss_frac = D(raw["per_position_loss_fraction"])
    except Exception as exc:
        raise PolicyError("policy amounts must be decimal strings") from exc
    if budget <= 0 or budget > D("1000"):
        raise PolicyError("session_budget_sol must be a positive paper amount up to 1000")
    if halt <= 0 or halt > budget:
        raise PolicyError("realized_loss_halt_sol must be positive and not above session budget")
    if loss_frac <= 0 or loss_frac >= 1:
        raise PolicyError("per_position_loss_fraction must be between 0 and 1")
    for key, lo, hi in (("max_hold_seconds", 60, 7 * 86400), ("quote_ttl_seconds", 1, 3600)):
        value = raw[key]
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            raise PolicyError(f"{key} is outside the simulator's technical bounds")
    screening = raw["screening"]
    if not isinstance(screening, dict):
        raise PolicyError("screening policy missing")
    if set(screening) != SCREEN_KEYS:
        raise PolicyError("screening policy must contain exactly the required thresholds")
    norm_screen = {}
    for key in SCREEN_KEYS:
        try:
            norm_screen[key] = D(screening[key])
        except Exception as exc:
            raise PolicyError(f"screening.{key} is not a number") from exc
        if norm_screen[key] <= 0:
            raise PolicyError(f"screening.{key} must be positive")
    for key in ("max_buy_tax", "max_sell_tax", "max_top10", "max_dev_hold", "max_bundler"):
        if norm_screen[key] > 1:
            raise PolicyError(f"screening.{key} cannot exceed 1")
    try:
        fee_model = normalize_fee_model(raw["fee_model"])
    except QuoteRejected as exc:
        raise PolicyError(exc.message) from exc
    # JSON-ready fee model (rates stay strings; bool stays bool).
    stored_fee = dict(raw["fee_model"])
    stored_fee["label"] = fee_model["label"]
    stored_fee["disclaimer"] = fee_model["disclaimer"]
    return {
        "preset_name": raw["preset_name"].strip(),
        "illustrative": True,
        "disclaimer_ack": DISCLAIMER_ACK,
        "notice": (
            "Illustrative paper numbers only. Not a risk recommendation, not approval "
            "to use real funds, and not read from any wallet balance."
        ),
        "max_positions": raw["max_positions"],
        "session_budget_sol": format(budget, "f"),
        "realized_loss_halt_sol": format(halt, "f"),
        "per_position_loss_fraction": format(loss_frac, "f"),
        "max_hold_seconds": raw["max_hold_seconds"],
        "quote_ttl_seconds": raw["quote_ttl_seconds"],
        "screening": {key: format(norm_screen[key], "f") for key in SCREEN_KEYS},
        "fee_model": stored_fee,
    }
