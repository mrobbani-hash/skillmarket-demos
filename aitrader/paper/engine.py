"""Paper book. Live orders and signing keys are not reachable from here."""

from __future__ import annotations

import json
import os
import threading
from decimal import Decimal
from pathlib import Path

from paper.fees import QuoteRejected, estimate_liquidation, filled_units, fit_entry
from paper.limits import (
    EXITS_NOT_GUARANTEED,
    GROSS_IS_NOT_NET,
    INITIAL_SIM_NOW,
    KILL_PHRASE,
    NET_TARGET,
    NOT_A_QUOTE,
    NOT_SAFE,
    PER_TRADE_CAP,
    RESET_PHRASE,
)
from paper.market import SyntheticMarket
from paper.money import D
from paper.presets import PolicyError, example_preset, validate_policy
from paper.screen import screen_snapshot

ACTIVE = {"open", "buy_ambiguous", "exit_ambiguous"}


class PaperEngine:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.market = SyntheticMarket()
        self.state = self._fresh()
        self._load()

    def close(self):
        return None

    def _fresh(self):
        return {
            "version": 1,
            "run": "stopped",
            "policies": None,
            "cash": None,
            "realized_pnl": "0",
            "realized_loss": "0",
            "positions": [],
            "events": [],
            "idempotency": {},
            "sim_now": INITIAL_SIM_NOW,
            "path_index": 0,
            "published_at": INITIAL_SIM_NOW,
            "buy_cursor": {},
            "sell_cursor": {},
            "next_position": 1,
            "next_event": 1,
            "load_error": None,
        }

    def _load(self):
        if not self.path.exists():
            self._sync_market()
            return
        try:
            raw = json.loads(self.path.read_text())
        except Exception as exc:
            self.state = self._fresh()
            self.state["load_error"] = f"paper state file is unreadable: {exc.__class__.__name__}"
            self._sync_market()
            return
        if not isinstance(raw, dict) or raw.get("version") != 1 or raw.get("run") not in {
            "stopped", "running", "paused", "killed"
        }:
            self.state = self._fresh()
            self.state["load_error"] = "paper state file was refused (unexpected contents)"
            self._sync_market()
            return
        if raw.get("mode") == "LIVE" or "api_key" in raw or "private_key" in raw:
            self.state = self._fresh()
            self.state["load_error"] = "refused a state file that looked like live trading config"
            self._sync_market()
            return
        self.state = raw
        self.state.setdefault("buy_cursor", {})
        self.state.setdefault("sell_cursor", {})
        self.state.setdefault("events", [])
        self.state.setdefault("idempotency", {})
        self.state.setdefault("positions", [])
        self._sync_market()

    def _sync_market(self):
        self.market.path_index = int(self.state["path_index"])
        self.market.published_at = int(self.state["published_at"])

    def _save(self):
        self.state["path_index"] = self.market.path_index
        self.state["published_at"] = self.market.published_at
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(self.state, separators=(",", ":"), sort_keys=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(blob)
        os.replace(tmp, self.path)

    def _event(self, kind: str, detail: dict):
        item = {
            "id": self.state["next_event"],
            "sim_now": self.state["sim_now"],
            "kind": kind,
            "detail": detail,
        }
        self.state["next_event"] += 1
        self.state["events"].append(item)
        if len(self.state["events"]) > 1000:
            self.state["events"] = self.state["events"][-1000:]
        return item

    def _replay(self, action: str, key: str):
        if not isinstance(key, str) or not key.strip():
            return _err("idempotency_key_required", "every mutation needs an idempotency key", 400)
        found = self.state["idempotency"].get(key)
        if found is None:
            return None
        if found.get("action") != action:
            return _err("idempotency_conflict", "this key was already used for a different action", 409)
        replay = dict(found["result"])
        replay["idempotent_replay"] = True
        return replay

    def _remember(self, action: str, key: str, result: dict):
        stored = dict(result)
        stored.pop("idempotent_replay", None)
        self.state["idempotency"][key] = {"action": action, "result": stored}
        if len(self.state["idempotency"]) > 500:
            for old in list(self.state["idempotency"])[:100]:
                self.state["idempotency"].pop(old, None)

    def _policies(self):
        return self.state.get("policies")

    def _cash(self) -> Decimal:
        if self.state["cash"] is None:
            return D(0)
        return D(self.state["cash"])

    def _exposure(self) -> Decimal:
        total = D(0)
        for pos in self.state["positions"]:
            if pos["state"] in ACTIVE:
                total += D(pos["committed"])
        return total

    def _active_count(self) -> int:
        return sum(1 for pos in self.state["positions"] if pos["state"] in ACTIVE)

    def _quote_fresh(self, snap) -> bool:
        policy = self._policies()
        if not policy or snap is None or snap.get("as_of") is None:
            return False
        age = int(self.state["sim_now"]) - int(snap["as_of"])
        return 0 <= age <= int(policy["quote_ttl_seconds"])

    def _snapshot(self, token_id):
        return self.market.snapshot(token_id, int(self.state["sim_now"]))

    def status(self) -> dict:
        with self._lock:
            return self._status_unlocked()

    def _status_unlocked(self) -> dict:
        policy = self._policies()
        positions = []
        for pos in self.state["positions"]:
            positions.append(self._view_position(pos))
        blockers = []
        if self.state.get("load_error"):
            blockers.append(self.state["load_error"])
        if policy is None:
            blockers.append("policies_not_configured")
        if self.state["run"] == "stopped":
            blockers.append("stopped")
        if self.state["run"] == "paused":
            blockers.append("paused")
        if self.state["run"] == "killed":
            blockers.append("killed")
        if policy and D(self.state["realized_loss"]) >= D(policy["realized_loss_halt_sol"]):
            blockers.append("realized_loss_halt")
        return {
            "paper_only": True,
            "badge": "PAPER ONLY",
            "live_orders": False,
            "live_trading_possible": False,
            "wallet_balance_read": False,
            "run": self.state["run"],
            "load_error": self.state.get("load_error"),
            "policies_configured": policy is not None,
            "policies": policy,
            "policy_notice": None if policy is None else policy.get("notice"),
            "blockers": blockers,
            "sim_now": self.state["sim_now"],
            "cash": self.state["cash"],
            "session_budget_sol": None if policy is None else policy["session_budget_sol"],
            "exposure": format(self._exposure(), "f"),
            "per_trade_cap": format(PER_TRADE_CAP, "f"),
            "net_target": format(NET_TARGET, "f"),
            "realized_pnl": self.state["realized_pnl"],
            "realized_loss": self.state["realized_loss"],
            "open_positions": self._active_count(),
            "positions": positions,
            "market": self.market.describe(),
            "limitations": [NOT_A_QUOTE, GROSS_IS_NOT_NET, NOT_SAFE, EXITS_NOT_GUARANTEED],
            "background_loop": False,
        }

    def _view_position(self, pos: dict) -> dict:
        view = dict(pos)
        view["exit_not_guaranteed"] = True
        if pos["state"] != "open" or not pos.get("filled_units"):
            view["estimate"] = {
                "actionable": False,
                "fresh": False,
                "available": False,
                "reason": "no_filled_units" if pos["state"] != "closed" else "closed",
                "net": None,
                "net_ratio": None,
                "gross_ratio": None,
            }
            return view
        snap = self._snapshot(pos["token_id"])
        policy = self._policies()
        ttl = int(policy["quote_ttl_seconds"]) if policy else 0
        price = None if snap is None else snap.get("price_sol")
        as_of = None if snap is None else snap.get("as_of")
        view["estimate"] = estimate_liquidation(pos, price, as_of, int(self.state["sim_now"]), ttl)
        view["estimate"]["gross_is_not_net"] = GROSS_IS_NOT_NET
        return view

    def ledger(self):
        with self._lock:
            return {"events": list(self.state["events"]), "paper_only": True}

    def candidates(self):
        with self._lock:
            policy = self._policies()
            rows = []
            for token_id in self.market.token_ids():
                snap = self._snapshot(token_id)
                decision = screen_snapshot(snap, policy, int(self.state["sim_now"]), self.market.unsupported)
                rows.append({
                    "token_id": token_id,
                    "symbol": snap["symbol"],
                    "label": "SYNTHETIC/PAPER",
                    "synthetic": True,
                    "real_candidate": False,
                    "price_sol": snap.get("price_sol"),
                    "quote_as_of": snap.get("as_of"),
                    "quote_fresh": self._quote_fresh(snap),
                    "creation_timestamp": snap.get("creation_timestamp"),
                    "open_timestamp_ignored": snap.get("open_timestamp"),
                    "screen": decision,
                })
            return {
                "paper_only": True,
                "source": self.market.describe(),
                "sim_now": self.state["sim_now"],
                "candidates": rows,
                "safety_certification": False,
            }

    def apply_example_preset(self):
        with self._lock:
            if self.state["run"] == "running":
                return _err("running", "pause or kill before replacing policies", 409)
            if self._active_count():
                return _err("positions_open", "reset the book before replacing policies", 409)
            policy = validate_policy(example_preset())
            self.state["policies"] = policy
            self.state["cash"] = policy["session_budget_sol"]
            self.state["realized_pnl"] = "0"
            self.state["realized_loss"] = "0"
            self.state["run"] = "stopped"
            self._event("example_preset_loaded", {"preset_name": policy["preset_name"], "illustrative": True})
            self._save()
            return {"ok": True, "http": 200, "status": self._status_unlocked()}

    def configure(self, raw: dict):
        with self._lock:
            if self.state["run"] == "running":
                return _err("running", "pause or kill before replacing policies", 409)
            if self._active_count():
                return _err("positions_open", "reset the book before replacing policies", 409)
            try:
                policy = validate_policy(raw)
            except PolicyError as exc:
                return _err("invalid_policy", exc.message, 400)
            self.state["policies"] = policy
            self.state["cash"] = policy["session_budget_sol"]
            self.state["realized_pnl"] = "0"
            self.state["realized_loss"] = "0"
            self.state["run"] = "stopped"
            self._event("policies_configured", {"preset_name": policy["preset_name"]})
            self._save()
            return {"ok": True, "http": 200, "status": self._status_unlocked()}

    def start(self):
        with self._lock:
            if self.state.get("load_error"):
                return _err("state_unreadable", self.state["load_error"], 409)
            if self._policies() is None:
                return _err("policies_not_configured", "set policies before start; the book stays stopped", 409)
            if self.state["run"] == "killed":
                return _err("killed", "reset the killed session before starting again", 409)
            if self.state["run"] not in {"stopped", "paused"}:
                return _err("running", "session is already running", 409)
            self.state["run"] = "running"
            self._event("started", {"note": "no entries are opened by start; nothing runs in the background"})
            self._save()
            return {"ok": True, "http": 200, "status": self._status_unlocked()}

    def pause(self):
        with self._lock:
            if self.state["run"] != "running":
                return _err("not_running", "pause applies to a running paper session", 409)
            self.state["run"] = "paused"
            self._event("paused", {})
            self._save()
            return {"ok": True, "http": 200, "status": self._status_unlocked()}

    def kill(self, confirm: str):
        with self._lock:
            if confirm != KILL_PHRASE:
                return _err("confirm_required", f"confirm must be {KILL_PHRASE}", 400)
            self.state["run"] = "killed"
            self._event("killed", {"note": "positions were not liquidated"})
            self._save()
            return {"ok": True, "http": 200, "status": self._status_unlocked()}

    def reset(self, confirm: str):
        with self._lock:
            if confirm != RESET_PHRASE:
                return _err("confirm_required", f"confirm must be {RESET_PHRASE}", 400)
            if self.state["run"] == "running":
                return _err("running", "pause or kill before reset", 409)
            policies = self.state.get("policies")
            fresh = self._fresh()
            fresh["policies"] = policies
            if policies:
                fresh["cash"] = policies["session_budget_sol"]
            fresh["load_error"] = None
            self.state = fresh
            self.market = SyntheticMarket()
            self._event("reset", {"policies_kept": policies is not None})
            self._save()
            return {"ok": True, "http": 200, "status": self._status_unlocked()}

    def open_position(self, token_id: str, idem_key: str):
        with self._lock:
            replay = self._replay("open", idem_key)
            if replay is not None:
                return replay
            result = self._open(token_id)
            if result.get("remember"):
                self._remember("open", idem_key, result)
            if result.get("remember") or result.get("mutated"):
                self._save()
            return result

    def _guard_running(self):
        if self.state["run"] == "killed":
            return _err("killed", "killed session is frozen and will not trade or step", 409)
        if self.state["run"] != "running":
            return _err("not_running", "paper session is not running", 409)
        return None

    def _open(self, token_id: str):
        blocked = self._guard_running()
        if blocked:
            return blocked
        policy = self._policies()
        if policy is None:
            return _err("policies_not_configured", "policies are not configured", 409)
        if D(self.state["realized_loss"]) >= D(policy["realized_loss_halt_sol"]):
            return _err("realized_loss_halt", "cumulative realized-loss halt is in force", 409)
        if self._active_count() >= int(policy["max_positions"]):
            return _err("max_positions", "configured max positions is already open", 409)
        snap = self._snapshot(token_id)
        if snap is None:
            return _err("unknown_token", "unknown synthetic token", 404)
        decision = screen_snapshot(snap, policy, int(self.state["sim_now"]), self.market.unsupported)
        if not decision["eligible"]:
            self._event("entry_skipped", {"token_id": token_id, "reasons": decision["reasons"]})
            return _err("screen_skip", "token skipped by fail-closed screen", 409, extra={"screen": decision, "mutated": True})
        if not self._quote_fresh(snap):
            return _err("stale_quote", "price quote is stale or missing; entry refused", 409)
        try:
            quote = fit_entry(policy["fee_model"], PER_TRADE_CAP)
            units = filled_units(quote["buy_input"], snap["price_sol"], quote["buy_slippage_rate"])
        except QuoteRejected as exc:
            return _err(exc.code, exc.message, 409)
        if D(quote["committed"]) > PER_TRADE_CAP or D(quote["buy_input"]) <= 0:
            return _err("over_cap", "quote does not fit the 0.04 envelope", 409)
        committed = D(quote["committed"])
        if self._cash() < committed:
            return _err("insufficient_funds", "paper cash is below the all-in commitment", 409)
        if self._exposure() + committed > D(policy["session_budget_sol"]):
            return _err("session_budget", "this commitment would exceed the configured session budget", 409)
        cursor = int(self.state["buy_cursor"].get(token_id, 0))
        outcome, new_cursor = self.market.scripted_outcome(token_id, "buy", cursor)
        if outcome == "unknown":
            return _err("unknown_fill", "buy outcome is unknown; no position was opened and nothing was marked filled", 409)
        self.state["buy_cursor"][token_id] = new_cursor
        if outcome == "fail":
            fee = D(quote["base_fee_sol"])
            self.state["cash"] = format(self._cash() - fee, "f")
            self.state["realized_loss"] = format(D(self.state["realized_loss"]) + fee, "f")
            self.state["realized_pnl"] = format(D(self.state["realized_pnl"]) - fee, "f")
            self._event("buy_failed", {"token_id": token_id, "charged_base_fee": format(fee, "f"), "position_opened": False})
            return {"ok": True, "http": 200, "remember": True, "outcome": "buy_failed", "position": None,
                    "charged_base_fee": format(fee, "f")}
        self.state["cash"] = format(self._cash() - committed, "f")
        state = "open" if outcome == "fill" else "buy_ambiguous" if outcome == "ambiguous" else None
        if state is None:
            return _err("unknown_fill", "buy outcome was not a known simulated result; no position opened", 409)
        pos = {
            "id": f"p-{self.state['next_position']}",
            "token_id": token_id,
            "symbol": snap["symbol"],
            "label": "SYNTHETIC/PAPER",
            "state": state,
            "opened_at": self.state["sim_now"],
            "entry_price_sol": snap["price_sol"],
            "filled_units": units if state == "open" else None,
            "committed": quote["committed"],
            "buy_input": quote["buy_input"],
            "buy_percent_fee": quote["buy_percent_fee"],
            "buy_fixed_fee": quote["buy_fixed_fee"],
            "ata_rent": quote["ata_rent"],
            "sell_reserve": quote["sell_reserve"],
            "sell_fixed_fee": quote["sell_fixed_fee"],
            "sell_percent_reserve": quote["sell_percent_reserve"],
            "gmgn_fee_rate": quote["gmgn_fee_rate"],
            "platform_fee_rate": quote["platform_fee_rate"],
            "buy_slippage_rate": quote["buy_slippage_rate"],
            "sell_slippage_rate": quote["sell_slippage_rate"],
            "base_fee_sol": quote["base_fee_sol"],
            "ata_reclaimed_on_full_sell": quote["ata_reclaimed_on_full_sell"],
            "fee_model_label": quote["label"],
            "quote_as_of_entry": snap["as_of"],
            "exit_reason": None,
            "exit_attempts": 0,
            "realized_net": None,
            "pending_exit": None,
            "closed_units": None,
        }
        self.state["next_position"] += 1
        self.state["positions"].append(pos)
        self._event("buy_" + ("filled" if state == "open" else "ambiguous"), {
            "position_id": pos["id"],
            "token_id": token_id,
            "committed": pos["committed"],
            "sell_reserve": pos["sell_reserve"],
            "filled_units": pos["filled_units"],
            "marked_closed": False,
        })
        return {"ok": True, "http": 200, "remember": True, "outcome": "buy_" + ("filled" if state == "open" else "ambiguous"),
                "position": self._view_position(pos)}

    def step(self, seconds: int, idem_key: str):
        with self._lock:
            replay = self._replay("step", idem_key)
            if replay is not None:
                return replay
            if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= 86400:
                return _err("bad_step", "seconds must be an integer from 1 to 86400", 400)
            blocked = self._guard_running()
            if blocked:
                return blocked
            self.state["sim_now"] = int(self.state["sim_now"]) + seconds
            self.market.advance(int(self.state["sim_now"]))
            exits = self._evaluate_unlocked()
            self._event("step", {"seconds": seconds, "exits": exits})
            result = {"ok": True, "http": 200, "remember": True, "exits": exits, "status": self._status_unlocked()}
            self._remember("step", idem_key, {"ok": True, "http": 200, "exits": exits})
            self._save()
            return result

    def evaluate_exits(self):
        """Test hook. Does not refresh quotes and does not advance time."""
        with self._lock:
            blocked = self._guard_running()
            if blocked:
                return blocked
            exits = self._evaluate_unlocked()
            self._save()
            return {"ok": True, "http": 200, "exits": exits}

    def _evaluate_unlocked(self):
        policy = self._policies()
        results = []
        if policy is None:
            return results
        loss_frac = D(policy["per_position_loss_fraction"])
        for pos in list(self.state["positions"]):
            if pos["state"] != "open":
                continue
            snap = self._snapshot(pos["token_id"])
            price = None if snap is None else snap.get("price_sol")
            as_of = None if snap is None else snap.get("as_of")
            est = estimate_liquidation(pos, price, as_of, int(self.state["sim_now"]), int(policy["quote_ttl_seconds"]))
            if not est.get("fresh") or not est.get("available") or est.get("net_ratio") is None:
                self._event("exit_not_executed", {
                    "position_id": pos["id"],
                    "reason": est.get("reason"),
                    "marked_closed": False,
                })
                results.append({"position_id": pos["id"], "result": "not_executed", "why": est.get("reason")})
                continue
            ratio = D(est["net_ratio"])
            held = int(self.state["sim_now"]) - int(pos["opened_at"])
            if ratio >= NET_TARGET and est.get("actionable"):
                why = "net_profit_target"
            elif ratio <= -loss_frac:
                why = "per_position_loss_trigger"
            elif held >= int(policy["max_hold_seconds"]):
                why = "max_hold_trigger"
            else:
                continue
            results.append(self._try_exit(pos, why, est))
        return results

    def request_exit(self, position_id: str, idem_key: str):
        with self._lock:
            replay = self._replay("exit", idem_key)
            if replay is not None:
                return replay
            blocked = self._guard_running()
            if blocked:
                return blocked
            pos = self._find(position_id)
            if pos is None or pos["state"] != "open":
                return _err("no_open_position", "that simulated position is not open", 404)
            policy = self._policies()
            snap = self._snapshot(pos["token_id"])
            est = estimate_liquidation(
                pos,
                None if snap is None else snap.get("price_sol"),
                None if snap is None else snap.get("as_of"),
                int(self.state["sim_now"]),
                int(policy["quote_ttl_seconds"]),
            )
            result = self._try_exit(pos, "manual", est)
            self._remember("exit", idem_key, {"ok": True, "http": 200, "exit": result})
            self._save()
            return {"ok": True, "http": 200, "exit": result}

    def resolve(self, position_id: str, outcome: str, idem_key: str):
        with self._lock:
            replay = self._replay("resolve", idem_key)
            if replay is not None:
                return replay
            if self.state["run"] == "killed":
                return _err("killed", "killed session does not resolve fills", 409)
            if outcome not in {"fill", "fail"}:
                return _err("bad_outcome", "resolution must be fill or fail", 400)
            pos = self._find(position_id)
            if pos is None or pos["state"] not in {"buy_ambiguous", "exit_ambiguous"}:
                return _err("not_ambiguous", "position is not in an ambiguous simulated state", 404)
            if pos["state"] == "buy_ambiguous" and outcome == "fail":
                self.state["cash"] = format(self._cash() + D(pos["committed"]), "f")
                pos["state"] = "void_unfilled"
                pos["exit_reason"] = "ambiguous_buy_resolved_unfilled"
                self._event("buy_ambiguity_resolved", {"position_id": pos["id"], "outcome": "fail", "marked_filled": False})
            elif pos["state"] == "buy_ambiguous" and outcome == "fill":
                if not pos.get("filled_units"):
                    pos["filled_units"] = filled_units(pos["buy_input"], pos["entry_price_sol"], pos["buy_slippage_rate"])
                pos["state"] = "open"
                self._event("buy_ambiguity_resolved", {
                    "position_id": pos["id"],
                    "outcome": "fill",
                    "filled_units": pos["filled_units"],
                    "note": "synthetic bookkeeping, not a chain confirmation",
                })
            elif pos["state"] == "exit_ambiguous" and outcome == "fail":
                pos["state"] = "open"
                pos["pending_exit"] = None
                self._event("exit_ambiguity_resolved", {"position_id": pos["id"], "outcome": "fail", "marked_closed": False})
            else:
                pending = pos.get("pending_exit") or {}
                if not pending.get("sol_back"):
                    return _err("stale_quote", "ambiguous exit has no snapshotted realizable proceeds; refusing to invent a fill", 409)
                self._book_close(pos, pending["reason"], pending["sol_back"], pending["net"])
                self._event("exit_ambiguity_resolved", {
                    "position_id": pos["id"],
                    "outcome": "fill",
                    "note": "applied the snapshotted paper estimate, not a new price and not a chain fill",
                })
            result = {"ok": True, "http": 200, "position": self._view_position(pos)}
            self._remember("resolve", idem_key, result)
            self._save()
            return result

    def _try_exit(self, pos, reason, est):
        if not est.get("fresh") or not est.get("available") or not est.get("actionable") or not est.get("sol_back"):
            pos["exit_attempts"] = int(pos.get("exit_attempts") or 0) + 1
            pos["last_exit_attempt"] = {"reason": reason, "result": "not_executed", "why": est.get("reason"), "at": self.state["sim_now"]}
            self._event("exit_not_executed", {
                "position_id": pos["id"],
                "trigger": reason,
                "why": est.get("reason"),
                "marked_closed": False,
                "note": EXITS_NOT_GUARANTEED,
            })
            return {"position_id": pos["id"], "result": "not_executed", "trigger": reason, "why": est.get("reason")}
        cursor = int(self.state["sell_cursor"].get(pos["token_id"], 0))
        outcome, new_cursor = self.market.scripted_outcome(pos["token_id"], "sell", cursor)
        self.state["sell_cursor"][pos["token_id"]] = new_cursor
        pos["exit_attempts"] = int(pos.get("exit_attempts") or 0) + 1
        if outcome == "fill":
            self._book_close(pos, reason, est["sol_back"], est["net"])
            self._event("exit_filled", {
                "position_id": pos["id"],
                "trigger": reason,
                "closed_units": pos["closed_units"],
                "realized_net": pos["realized_net"],
                "note": "simulated fill of this position's units only",
            })
            return {"position_id": pos["id"], "result": "filled", "trigger": reason, "realized_net": pos["realized_net"]}
        if outcome == "ambiguous":
            pos["state"] = "exit_ambiguous"
            pos["pending_exit"] = {
                "reason": reason,
                "sol_back": est["sol_back"],
                "net": est["net"],
                "net_ratio": est["net_ratio"],
                "at": self.state["sim_now"],
            }
            self._event("exit_ambiguous", {
                "position_id": pos["id"],
                "trigger": reason,
                "marked_closed": False,
                "note": EXITS_NOT_GUARANTEED,
            })
            return {"position_id": pos["id"], "result": "ambiguous", "trigger": reason}
        pos["last_exit_attempt"] = {"reason": reason, "result": "failed", "outcome": outcome, "at": self.state["sim_now"]}
        self._event("exit_failed", {
            "position_id": pos["id"],
            "trigger": reason,
            "outcome": outcome,
            "marked_closed": False,
            "note": EXITS_NOT_GUARANTEED,
        })
        return {"position_id": pos["id"], "result": "failed", "trigger": reason, "outcome": outcome}

    def _book_close(self, pos, reason, sol_back, net):
        self.state["cash"] = format(self._cash() + D(sol_back), "f")
        net_d = D(net)
        self.state["realized_pnl"] = format(D(self.state["realized_pnl"]) + net_d, "f")
        if net_d < 0:
            self.state["realized_loss"] = format(D(self.state["realized_loss"]) + (-net_d), "f")
        pos["state"] = "closed"
        pos["exit_reason"] = reason
        pos["realized_net"] = format(net_d, "f")
        pos["closed_units"] = pos["filled_units"]
        pos["pending_exit"] = None

    def _find(self, position_id):
        for pos in self.state["positions"]:
            if pos["id"] == position_id:
                return pos
        return None


def _err(code, message, http, extra=None):
    body = {"ok": False, "http": http, "error": code, "message": message, "remember": False}
    if extra:
        body.update(extra)
    return body
