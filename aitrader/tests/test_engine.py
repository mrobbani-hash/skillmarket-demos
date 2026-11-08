from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

from paper.engine import PaperEngine
from paper.fees import fit_entry
from paper.limits import KILL_PHRASE, NET_TARGET, PER_TRADE_CAP, RESET_PHRASE
from paper.presets import example_preset, validate_policy


def boot(tmp_path, name="state.json"):
    eng = PaperEngine(tmp_path / name)
    assert eng.apply_example_preset()["ok"] is True
    assert eng.start()["ok"] is True
    return eng


def test_default_is_stopped_without_policies(tmp_path):
    eng = PaperEngine(tmp_path / "state.json")
    status = eng.status()
    assert status["run"] == "stopped"
    assert status["policies_configured"] is False
    assert status["cash"] is None
    assert "policies_not_configured" in status["blockers"]
    assert eng.start()["error"] == "policies_not_configured"
    assert eng.open_position("SYN-ALPHA", "k")["error"] == "not_running"
    assert status["session_budget_sol"] is None


def test_open_sizes_to_cap_and_rejects_ineligible(tmp_path):
    eng = boot(tmp_path)
    before = Decimal(eng.status()["cash"])
    skipped = eng.open_position("SYN-OLD", "old")
    assert skipped["error"] == "screen_skip"
    assert Decimal(eng.status()["cash"]) == before
    pool = eng.open_position("SYN-POOL-OPEN", "pool")
    assert pool["error"] == "screen_skip"
    unknown = eng.open_position("SYN-UNKNOWN", "unk")
    assert unknown["error"] == "screen_skip"
    opened = eng.open_position("SYN-ALPHA", "a")
    assert opened["outcome"] == "buy_filled"
    pos = opened["position"]
    assert Decimal(pos["committed"]) <= PER_TRADE_CAP
    assert Decimal(pos["buy_input"]) > 0
    assert Decimal(pos["sell_reserve"]) > 0
    assert pos["filled_units"]
    assert pos["label"] == "SYNTHETIC/PAPER"
    assert Decimal(eng.status()["cash"]) == before - Decimal(pos["committed"])


def test_idempotent_open_and_failed_buy_and_ambiguous(tmp_path):
    eng = boot(tmp_path)
    first = eng.open_position("SYN-ALPHA", "same")
    second = eng.open_position("SYN-ALPHA", "same")
    assert second["idempotent_replay"] is True
    assert eng.status()["open_positions"] == 1
    cash_after_first = eng.status()["cash"]
    assert eng.open_position("SYN-ALPHA", "same")["position"]["id"] == first["position"]["id"]
    assert eng.status()["cash"] == cash_after_first
    budget = Decimal(eng.status()["session_budget_sol"])
    failed = eng.open_position("SYN-BUYFAIL", "fail")
    assert failed["outcome"] == "buy_failed"
    assert failed["position"] is None
    charged = Decimal(failed["charged_base_fee"])
    assert charged == Decimal("0.000005")
    assert Decimal(eng.status()["cash"]) == Decimal(cash_after_first) - charged
    assert Decimal(eng.status()["realized_loss"]) == charged
    amb = eng.open_position("SYN-AMBIG", "amb")
    assert amb["position"]["state"] == "buy_ambiguous"
    assert amb["position"]["filled_units"] is None
    assert eng.status()["open_positions"] == 2
    resolved = eng.resolve(amb["position"]["id"], "fail", "res")
    assert resolved["position"]["state"] == "void_unfilled"
    assert Decimal(eng.status()["cash"]) == budget - Decimal(first["position"]["committed"]) - charged


def test_session_cap_max_positions_and_insufficient_funds(tmp_path):
    eng = boot(tmp_path)
    assert eng.open_position("SYN-ALPHA", "1")["ok"]
    assert eng.open_position("SYN-BETA", "2")["ok"]
    assert eng.open_position("SYN-GAMMA", "3")["ok"]
    blocked = eng.open_position("SYN-LOSS", "4")
    assert blocked["error"] == "max_positions"
    custom = dict(example_preset())
    custom["preset_name"] = "TIGHT"
    custom["max_positions"] = 5
    quote = fit_entry(custom["fee_model"])
    custom["session_budget_sol"] = quote["committed"]
    custom["realized_loss_halt_sol"] = "0.0001"
    path = tmp_path / "tight.json"
    eng2 = PaperEngine(path)
    assert eng2.configure(custom)["ok"]
    assert eng2.start()["ok"]
    assert eng2.open_position("SYN-ALPHA", "a")["ok"]
    second = eng2.open_position("SYN-BETA", "b")
    assert second["error"] in {"session_budget", "insufficient_funds"}
    tiny = dict(example_preset())
    tiny["preset_name"] = "TINY"
    tiny["session_budget_sol"] = "0.01"
    tiny["realized_loss_halt_sol"] = "0.005"
    eng3 = PaperEngine(tmp_path / "tiny.json")
    eng3.configure(tiny)
    eng3.start()
    refused = eng3.open_position("SYN-ALPHA", "a")
    assert refused["error"] in {"session_budget", "insufficient_funds"}
    assert Decimal(eng3.status()["cash"]) == Decimal("0.01")


def test_net_target_not_gross_and_only_that_position(tmp_path):
    eng = boot(tmp_path)
    alpha = eng.open_position("SYN-ALPHA", "a")["position"]
    beta = eng.open_position("SYN-BETA", "b")["position"]
    first = eng.step(60, "s1")
    assert first["exits"] == []
    view = {p["id"]: p for p in eng.status()["positions"]}
    assert view[alpha["id"]]["state"] == "open"
    assert Decimal(view[alpha["id"]]["estimate"]["gross_ratio"]) == Decimal("0.2")
    assert Decimal(view[alpha["id"]]["estimate"]["net_ratio"]) < NET_TARGET
    assert view[beta["id"]]["state"] == "open"
    second = eng.step(60, "s1")
    assert second["idempotent_replay"] is True
    assert {p["state"] for p in eng.status()["positions"]} == {"open"}
    done = eng.step(60, "s2")
    assert any(item["result"] == "filled" and item["trigger"] == "net_profit_target" for item in done["exits"])
    view = {p["id"]: p for p in eng.status()["positions"]}
    assert view[alpha["id"]]["state"] == "closed"
    assert view[alpha["id"]]["exit_reason"] == "net_profit_target"
    assert view[alpha["id"]]["closed_units"] == alpha["filled_units"]
    assert Decimal(view[alpha["id"]]["realized_net"]) / Decimal(alpha["committed"]) >= NET_TARGET
    assert view[beta["id"]]["state"] == "open"
    assert view[beta["id"]]["filled_units"] == beta["filled_units"]


def test_loss_and_time_triggers_can_fail_without_closing(tmp_path):
    custom = dict(example_preset())
    custom["preset_name"] = "TRIGGERS"
    custom["max_hold_seconds"] = 60
    eng = PaperEngine(tmp_path / "t.json")
    eng.configure(custom)
    eng.start()
    loss = eng.open_position("SYN-LOSS", "loss")["position"]
    stepped = eng.step(60, "step")
    assert any(item["position_id"] == loss["id"] and item["trigger"] == "per_position_loss_trigger" for item in stepped["exits"])
    closed = next(p for p in eng.status()["positions"] if p["id"] == loss["id"])
    assert closed["state"] == "closed"
    assert closed["exit_reason"] == "per_position_loss_trigger"
    eng.market.rows["SYN-FAILSELL"]["sell_script"] = ["fail", "fail", "fail", "fail"]
    held = eng.open_position("SYN-FAILSELL", "hold")["position"]
    # Price path for FAILSELL stays flat, so the next step is the hold trigger.
    failed = eng.step(60, "hold-step")
    assert any(item["position_id"] == held["id"] and item["result"] == "failed" for item in failed["exits"])
    still = next(p for p in eng.status()["positions"] if p["id"] == held["id"])
    assert still["state"] == "open"
    assert still["realized_net"] is None
    assert "guaranteed" not in (still["exit_reason"] or "")


def test_stale_quote_does_not_close_and_manual_fail_stays_open(tmp_path):
    eng = boot(tmp_path)
    pos = eng.open_position("SYN-ALPHA", "a")["position"]
    eng.market.published_at = eng.state["sim_now"] - 1000
    result = eng.evaluate_exits()
    assert result["exits"][0]["result"] == "not_executed"
    assert next(p for p in eng.status()["positions"] if p["id"] == pos["id"])["state"] == "open"
    eng2 = boot(tmp_path, "m.json")
    held = eng2.open_position("SYN-FAILSELL", "f")["position"]
    manual = eng2.request_exit(held["id"], "exit")
    assert manual["exit"]["result"] == "failed"
    assert next(p for p in eng2.status()["positions"] if p["id"] == held["id"])["state"] == "open"


def test_ambiguous_exit_resolve_uses_snapshot_not_a_new_price(tmp_path):
    eng = boot(tmp_path)
    eng.market.rows["SYN-BETA"]["sell_script"] = ["ambiguous"]
    pos = eng.open_position("SYN-BETA", "b")["position"]
    eng.request_exit(pos["id"], "exit")
    pending = next(p for p in eng.state["positions"] if p["id"] == pos["id"])
    assert pending["state"] == "exit_ambiguous"
    snap_back = pending["pending_exit"]["sol_back"]
    eng.market.rows["SYN-BETA"]["path"] = ["9", "9", "9"]
    eng.market.path_index = 2
    cash_before = Decimal(eng.status()["cash"])
    resolved = eng.resolve(pos["id"], "fill", "resolve")
    assert resolved["position"]["state"] == "closed"
    assert Decimal(eng.status()["cash"]) - cash_before == Decimal(snap_back)


def test_restart_persists_caps_kill_and_idempotency(tmp_path):
    path = tmp_path / "state.json"
    eng = PaperEngine(path)
    eng.apply_example_preset()
    eng.start()
    opened = eng.open_position("SYN-BETA", "persist")
    cash = eng.status()["cash"]
    exposure = eng.status()["exposure"]
    eng.kill(KILL_PHRASE)
    revived = PaperEngine(path)
    assert revived.status()["run"] == "killed"
    assert revived.status()["cash"] == cash
    assert revived.status()["exposure"] == exposure
    assert revived.status()["positions"][0]["id"] == opened["position"]["id"]
    assert revived.step(60, "nope")["error"] == "killed"
    assert revived.open_position("SYN-GAMMA", "g")["error"] == "killed"
    replay = revived.open_position("SYN-BETA", "persist")
    assert replay["idempotent_replay"] is True
    assert revived.status()["open_positions"] == 1
    assert revived.reset("no")["error"] == "confirm_required"
    assert revived.reset(RESET_PHRASE)["ok"]
    assert revived.status()["run"] == "stopped"
    assert revived.status()["positions"] == []
    assert revived.status()["policies_configured"] is True
    assert Decimal(revived.status()["cash"]) == Decimal(revived.status()["session_budget_sol"])


def test_kill_does_not_liquidate_and_pause_blocks(tmp_path):
    eng = boot(tmp_path)
    pos = eng.open_position("SYN-BETA", "b")["position"]
    assert eng.kill("no")["error"] == "confirm_required"
    assert eng.kill(KILL_PHRASE)["ok"]
    assert eng.status()["positions"][0]["state"] == "open"
    assert eng.status()["positions"][0]["id"] == pos["id"]
    assert eng.step(60, "s")["error"] == "killed"
    eng2 = boot(tmp_path, "p.json")
    assert eng2.reset(RESET_PHRASE)["error"] == "running"
    eng2.pause()
    assert eng2.status()["run"] == "paused"
    assert eng2.open_position("SYN-ALPHA", "a")["error"] == "not_running"
    assert eng2.step(60, "s")["error"] == "not_running"
    assert eng2.reset(RESET_PHRASE)["ok"]
    assert eng2.status()["run"] == "stopped"


def test_concurrent_opens_respect_cap_and_idempotency(tmp_path):
    eng = boot(tmp_path)
    tokens = ["SYN-ALPHA", "SYN-BETA", "SYN-GAMMA", "SYN-LOSS"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: eng.open_position(tokens[i], f"c{i}"), range(4)))
    filled = [item for item in results if item.get("outcome") == "buy_filled"]
    assert len(filled) == 3
    assert eng.status()["open_positions"] == 3
    assert Decimal(eng.status()["cash"]) >= 0
    assert Decimal(eng.status()["exposure"]) <= Decimal(eng.status()["session_budget_sol"])
    eng2 = boot(tmp_path, "idem.json")
    with ThreadPoolExecutor(max_workers=2) as pool:
        pair = list(pool.map(lambda _n: eng2.open_position("SYN-ALPHA", "same-key"), (1, 2)))
    assert eng2.status()["open_positions"] == 1
    assert sum(1 for item in pair if item.get("idempotent_replay")) == 1


def test_corrupt_state_stays_stopped(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    eng = PaperEngine(path)
    assert eng.status()["load_error"]
    assert eng.status()["run"] == "stopped"
    assert eng.start()["error"] == "state_unreadable"
    assert eng.open_position("SYN-ALPHA", "a")["error"] == "not_running"


def test_loss_halt_blocks_new_entries(tmp_path):
    custom = validate_policy({
        **example_preset(),
        "preset_name": "HALT",
        "realized_loss_halt_sol": "0.000005",
        "session_budget_sol": "0.12",
    })
    eng = PaperEngine(tmp_path / "h.json")
    eng.state["policies"] = custom
    eng.state["cash"] = custom["session_budget_sol"]
    eng.state["run"] = "running"
    failed = eng.open_position("SYN-BUYFAIL", "f")
    assert failed["outcome"] == "buy_failed"
    halted = eng.open_position("SYN-ALPHA", "a")
    assert halted["error"] == "realized_loss_halt"


def test_policy_validator_rejects_partial_and_live_fields():
    raw = example_preset()
    raw.pop("max_hold_seconds")
    from paper.presets import PolicyError
    import pytest
    with pytest.raises(PolicyError):
        validate_policy(raw)
    raw = example_preset()
    raw["wallet_balance"] = "1.18"
    with pytest.raises(PolicyError):
        validate_policy(raw)
