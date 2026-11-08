# Paper-only simulator

This entry is a separate process from `app.py`. It cannot place a live order, sign a request, read a GMGN key, or call `gmgn-cli`.

`app.py` is still the upstream live-capable demo. Do not run it for this paper workflow.

## Run

From `aitrader`, with the pinned paper requirements installed:

```bash
python -m paper
```

The process binds `127.0.0.1:8000` only. Open `http://127.0.0.1:8000`.

The book starts **stopped** with no risk numbers filled in. Load the illustrative fake-money example, press Start, then use Simulate entry and Step. Nothing runs in the background.

## Rules that are fixed

- Each simulated trade locks at most **0.04 paper SOL**, including buy input, modeled fees, and a sell reserve.
- A full simulated exit is considered only when estimated realizable **net / committed all-in** reaches 20%. A 20% gross price change is not that test.
- Only that position's filled units are closed.
- Token age uses `creation_timestamp` and must be under 1 hour. Pool-open time does not count.
- Missing, stale, or unsupported safety fields skip the row. They are not treated as safe.

## Rules you must set

Max positions, session budget, cumulative realized-loss halt, per-position loss fraction, and max holding time are **not** inferred from a wallet. The example preset is labeled illustrative and is not a recommendation. Its paper budget is 0.12, which is three times 0.04, not a live balance.

## Tests

```bash
python -m pytest
```

## Honest limits

- The fee model is a configured illustration, not an executable GMGN quote.
- Fixture rows are SYNTHETIC/PAPER. They are not real candidates, prices, or verified security.
- Real LP withdrawal history and real token-2022 account state are not read. The fixture fabricates those fields so the fail-closed checks can be exercised. A source that marks them unsupported skips every row.
- A loss trigger or a max-hold trigger only attempts a simulated sell. The attempt can fail or stay ambiguous.
- Failed and ambiguous buys are not marked filled. Ambiguous exits are not marked closed.
- There is no promise of profit or of being able to exit.
