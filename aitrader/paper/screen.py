"""Fail-closed synthetic screening. Missing never becomes a passing zero."""

from paper.limits import MAX_LAUNCH_AGE_SECONDS, NOT_SAFE
from paper.money import D

# Fields a snapshot must actually contain. open_timestamp is deliberately absent.
REQUIRED_FIELDS = (
    "creation_timestamp",
    "renounced_mint",
    "renounced_freeze",
    "honeypot",
    "buy_tax",
    "sell_tax",
    "liquidity_usd",
    "lp_burned",
    "lp_lock_ratio",
    "lp_withdrawal_count",
    "top10_rate",
    "dev_hold_rate",
    "dev_address",
    "bundler_rate",
    "is_wash_trading",
    "volume_usd",
    "token2022_permanent_delegate",
    "token2022_transfer_fee_bps",
    "price_sol",
    "as_of",
    "source_id",
    "synthetic",
)


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return D(value)
    except Exception:
        return None


def _check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def coverage(snapshot: dict, unsupported: set[str]) -> dict:
    out = {}
    for field in REQUIRED_FIELDS:
        if field in unsupported:
            out[field] = "unsupported"
        elif not isinstance(snapshot, dict) or field not in snapshot or snapshot[field] is None:
            out[field] = "missing"
        else:
            out[field] = "present"
    out["open_timestamp"] = "ignored"
    return out


def screen_snapshot(snapshot: dict, policy: dict | None, sim_now: int, unsupported: set[str] | None = None) -> dict:
    unsupported = unsupported or set()
    snapshot = snapshot or {}
    checks = []
    reasons = []

    def add(check):
        checks.append(check)
        if check["status"] != "pass":
            reasons.append(check["name"] + ":" + check["status"])

    if "creation_timestamp" in unsupported:
        add(_check("launch_age", "unsupported", "source cannot prove token creation time"))
    else:
        created = _num(snapshot.get("creation_timestamp"))
        if created is None:
            add(_check("launch_age", "unknown", "creation_timestamp missing; pool-open time is not used"))
        else:
            age = D(sim_now) - created
            if age <= 0 or age >= MAX_LAUNCH_AGE_SECONDS:
                add(_check("launch_age", "fail", f"creation age {age}s is not under 3600s"))
            else:
                add(_check("launch_age", "pass", f"creation age {age}s"))

    def flag(name, key, want, fail_detail, ok_detail):
        if key in unsupported:
            add(_check(name, "unsupported", f"{key} is unsupported by this source"))
            return
        if key not in snapshot or snapshot.get(key) is None:
            add(_check(name, "unknown", f"{key} missing"))
            return
        if snapshot.get(key) is want:
            add(_check(name, "pass", ok_detail))
        else:
            add(_check(name, "fail", fail_detail))

    flag("renounced_mint", "renounced_mint", True, "mint authority not renounced", "mint renounced")
    flag("renounced_freeze", "renounced_freeze", True, "freeze authority not renounced", "freeze renounced")
    flag("honeypot", "honeypot", False, "honeypot true", "honeypot explicitly false")
    flag("wash", "is_wash_trading", False, "wash trading flagged", "wash flag explicitly false")
    flag(
        "token2022_delegate",
        "token2022_permanent_delegate",
        False,
        "permanent delegate set",
        "permanent delegate explicitly false",
    )

    def bounded(name, key, ceiling, higher_is_worse=True, floor=None):
        if key in unsupported:
            add(_check(name, "unsupported", f"{key} unsupported"))
            return
        if policy is None or ceiling is None:
            add(_check(name, "unknown", "threshold not configured"))
            return
        value = _num(snapshot.get(key)) if key in snapshot else None
        if value is None:
            add(_check(name, "unknown", f"{key} missing"))
            return
        if higher_is_worse and value > D(ceiling):
            add(_check(name, "fail", f"{key} {value} above {ceiling}"))
        elif floor is not None and value < D(floor):
            add(_check(name, "fail", f"{key} {value} below {floor}"))
        else:
            add(_check(name, "pass", f"{key} {value}"))

    screening = (policy or {}).get("screening") if policy else None
    bounded("buy_tax", "buy_tax", None if not screening else screening.get("max_buy_tax"))
    bounded("sell_tax", "sell_tax", None if not screening else screening.get("max_sell_tax"))
    bounded("top10", "top10_rate", None if not screening else screening.get("max_top10"))
    bounded("dev_hold", "dev_hold_rate", None if not screening else screening.get("max_dev_hold"))
    bounded("bundler", "bundler_rate", None if not screening else screening.get("max_bundler"))
    if not screening:
        add(_check("liquidity", "unknown", "threshold not configured"))
        add(_check("volume", "unknown", "policy not configured"))
    else:
        bounded("liquidity", "liquidity_usd", "1e100", higher_is_worse=False, floor=screening.get("min_liquidity_usd"))
        bounded("volume", "volume_usd", "1e100", higher_is_worse=False, floor="0.000000001")

    if "lp_burned" in unsupported or "lp_lock_ratio" in unsupported:
        add(_check("lp_control", "unsupported", "LP burn/lock unsupported"))
    elif "lp_burned" not in snapshot or snapshot.get("lp_burned") is None or "lp_lock_ratio" not in snapshot or snapshot.get("lp_lock_ratio") is None:
        add(_check("lp_control", "unknown", "LP burn and lock ratio are both required"))
    else:
        burned = snapshot.get("lp_burned")
        lock = _num(snapshot.get("lp_lock_ratio"))
        if burned is True and lock is not None and lock >= 0:
            add(_check("lp_control", "pass", "LP burned"))
        elif burned is False and lock is not None and lock == 1:
            add(_check("lp_control", "pass", "LP fully locked"))
        elif lock is None or not isinstance(burned, bool):
            add(_check("lp_control", "unknown", "LP burn/lock unreadable"))
        else:
            add(_check("lp_control", "fail", "LP is neither burned nor fully locked"))

    if "lp_withdrawal_count" in unsupported:
        add(_check("lp_withdrawals", "unsupported", "withdrawal history unsupported; skipped, not treated as clean"))
    else:
        count = snapshot.get("lp_withdrawal_count") if "lp_withdrawal_count" in snapshot else None
        number = _num(count)
        if number is None or isinstance(count, bool):
            add(_check("lp_withdrawals", "unknown", "lp_withdrawal_count missing"))
        elif number != 0:
            add(_check("lp_withdrawals", "fail", f"withdrawal count {number}"))
        else:
            add(_check("lp_withdrawals", "pass", "explicit withdrawal count 0"))

    if "token2022_transfer_fee_bps" in unsupported:
        add(_check("token2022_transfer_fee", "unsupported", "transfer-fee extension unsupported"))
    else:
        fee_bps = snapshot.get("token2022_transfer_fee_bps") if "token2022_transfer_fee_bps" in snapshot else None
        number = _num(fee_bps)
        if number is None:
            add(_check("token2022_transfer_fee", "unknown", "transfer fee bps missing"))
        elif number != 0:
            add(_check("token2022_transfer_fee", "fail", f"transfer fee {number} bps"))
        else:
            add(_check("token2022_transfer_fee", "pass", "transfer fee explicitly 0"))

    if "dev_address" in unsupported:
        add(_check("dev_address", "unsupported", "dev address unsupported"))
    elif not isinstance(snapshot.get("dev_address"), str) or not snapshot.get("dev_address").strip():
        add(_check("dev_address", "unknown", "dev address missing"))
    else:
        add(_check("dev_address", "pass", "dev address present"))

    if "price_sol" in unsupported:
        add(_check("price", "unsupported", "price unsupported"))
    else:
        price = _num(snapshot.get("price_sol")) if "price_sol" in snapshot else None
        if price is None or price <= 0:
            add(_check("price", "unknown", "price missing or not positive"))
        else:
            add(_check("price", "pass", "price present"))

    if "as_of" in unsupported or snapshot.get("as_of") is None:
        add(_check("quote_timestamp", "unknown", "quote timestamp missing"))
    else:
        add(_check("quote_timestamp", "pass", "quote timestamp present"))

    if snapshot.get("synthetic") is True and snapshot.get("source_id"):
        add(_check("synthetic_source", "pass", "labeled synthetic fixture"))
    else:
        add(_check("synthetic_source", "fail", "this runtime refuses unlabeled or non-synthetic snapshots"))

    eligible = all(item["status"] == "pass" for item in checks)
    return {
        "eligible": eligible,
        "decision": "eligible_for_synthetic_paper" if eligible else "skip",
        "reasons": reasons,
        "checks": checks,
        "safety_certification": False,
        "notice": NOT_SAFE,
        "ignored_open_timestamp": "open_timestamp" in snapshot,
        "coverage": coverage(snapshot, unsupported),
    }
