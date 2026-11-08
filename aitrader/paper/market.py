"""Synthetic fixture book. Every row is fabricated and labeled as such."""

from paper.limits import INITIAL_SIM_NOW

DOCUMENTED_GAPS = (
    "No live GMGN quote is fetched. The fee model is an illustration.",
    "Real LP withdrawal history is not read. The fixture's withdrawal count is fabricated.",
    "Token-2022 extension state is not read from chain. Fixture flags are fabricated.",
    "Wash, holder, dev, and liquidity fields on these rows are not market data.",
    "open_timestamp is ignored and is never treated as token launch time.",
)


def _complete(**overrides):
    row = {
        "symbol": "SYN",
        "synthetic": True,
        "source_id": "synthetic-fixture",
        "creation_timestamp": INITIAL_SIM_NOW - 600,
        "open_timestamp": INITIAL_SIM_NOW - 5000,
        "renounced_mint": True,
        "renounced_freeze": True,
        "honeypot": False,
        "buy_tax": "0",
        "sell_tax": "0",
        "liquidity_usd": "50000",
        "lp_burned": True,
        "lp_lock_ratio": "1",
        "lp_withdrawal_count": 0,
        "top10_rate": "0.20",
        "dev_hold_rate": "0.02",
        "dev_address": "SYNTHETIC_DEV",
        "bundler_rate": "0.05",
        "is_wash_trading": False,
        "volume_usd": "10000",
        "token2022_permanent_delegate": False,
        "token2022_transfer_fee_bps": 0,
        "price0": "0.00001",
        "path": ["1", "1", "1"],
        "buy_script": ["fill"],
        "sell_script": ["fill"],
    }
    row.update(overrides)
    return row


def fixture_rows():
    return [
        _complete(token_id="SYN-ALPHA", symbol="SYN-ALPHA", path=["1", "1.20", "1.80"]),
        _complete(token_id="SYN-BETA", symbol="SYN-BETA"),
        _complete(token_id="SYN-GAMMA", symbol="SYN-GAMMA"),
        _complete(token_id="SYN-FAILSELL", symbol="SYN-FAILSELL", sell_script=["fail", "fail", "fail", "fail"]),
        _complete(token_id="SYN-AMBIG", symbol="SYN-AMBIG", buy_script=["ambiguous"]),
        _complete(token_id="SYN-BUYFAIL", symbol="SYN-BUYFAIL", buy_script=["fail"]),
        _complete(token_id="SYN-LOSS", symbol="SYN-LOSS", path=["1", "0.20", "0.20"]),
        _complete(
            token_id="SYN-OLD",
            symbol="SYN-OLD",
            creation_timestamp=INITIAL_SIM_NOW - 7200,
        ),
        _complete(
            token_id="SYN-POOL-OPEN",
            symbol="SYN-POOL-OPEN",
            creation_timestamp=None,
            open_timestamp=INITIAL_SIM_NOW - 60,
        ),
        _complete(
            token_id="SYN-UNKNOWN",
            symbol="SYN-UNKNOWN",
            honeypot=None,
            top10_rate=None,
            is_wash_trading=None,
        ),
        _complete(token_id="SYN-WASH", symbol="SYN-WASH", is_wash_trading=True),
        _complete(token_id="SYN-RUGGED", symbol="SYN-RUGGED", lp_withdrawal_count=3, lp_burned=False, lp_lock_ratio="0.2"),
    ]


class SyntheticMarket:
    """In-memory fixture. path_index and published_at are owned by the engine."""

    def __init__(self):
        self.rows = {row["token_id"]: row for row in fixture_rows()}
        self.path_index = 0
        self.published_at = INITIAL_SIM_NOW
        self.unsupported: set[str] = set()

    def describe(self):
        return {
            "source_id": "synthetic-fixture",
            "synthetic": True,
            "label": "SYNTHETIC/PAPER",
            "published_at": self.published_at,
            "path_index": self.path_index,
            "documented_gaps": list(DOCUMENTED_GAPS),
            "unsupported_fields": sorted(self.unsupported),
            "real_candidates": False,
            "real_prices": False,
            "verified_security": False,
        }

    def advance(self, sim_now: int):
        self.path_index += 1
        self.published_at = sim_now

    def token_ids(self):
        return list(self.rows)

    def snapshot(self, token_id: str, sim_now: int):
        row = self.rows.get(token_id)
        if row is None:
            return None
        path = row["path"]
        index = min(self.path_index, len(path) - 1)
        from decimal import Decimal
        price = Decimal(row["price0"]) * Decimal(path[index])
        snap = {
            "token_id": token_id,
            "symbol": row["symbol"],
            "synthetic": True,
            "label": "SYNTHETIC/PAPER",
            "source_id": "synthetic-fixture",
            "creation_timestamp": row["creation_timestamp"],
            "open_timestamp": row["open_timestamp"],
            "renounced_mint": row["renounced_mint"],
            "renounced_freeze": row["renounced_freeze"],
            "honeypot": row["honeypot"],
            "buy_tax": row["buy_tax"],
            "sell_tax": row["sell_tax"],
            "liquidity_usd": row["liquidity_usd"],
            "lp_burned": row["lp_burned"],
            "lp_lock_ratio": row["lp_lock_ratio"],
            "lp_withdrawal_count": row["lp_withdrawal_count"],
            "top10_rate": row["top10_rate"],
            "dev_hold_rate": row["dev_hold_rate"],
            "dev_address": row["dev_address"],
            "bundler_rate": row["bundler_rate"],
            "is_wash_trading": row["is_wash_trading"],
            "volume_usd": row["volume_usd"],
            "token2022_permanent_delegate": row["token2022_permanent_delegate"],
            "token2022_transfer_fee_bps": row["token2022_transfer_fee_bps"],
            "price_sol": format(price, "f"),
            "price_multiplier": path[index],
            "as_of": self.published_at,
            "sim_now": sim_now,
        }
        # Preserve explicit Nones so screening can see missing fields.
        for key, value in list(snap.items()):
            if value is None:
                snap[key] = None
        return snap

    def scripted_outcome(self, token_id: str, side: str, cursor: int) -> tuple[str, int]:
        row = self.rows.get(token_id)
        if row is None:
            return "unknown", cursor
        script = row["buy_script"] if side == "buy" else row["sell_script"]
        if cursor < 0 or cursor >= len(script):
            return "unknown", cursor
        return script[cursor], cursor + 1
