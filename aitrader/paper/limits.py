"""Fixed paper-sim rules. These are not a live quote and not a wallet balance."""

from decimal import Decimal

# One simulated trade may lock at most this much paper SOL, all-in.
PER_TRADE_CAP = Decimal("0.04")
# Exit only when estimated realizable net / committed all-in reaches this.
NET_TARGET = Decimal("0.20")
# Genuine token-creation age. Pool-open time is never accepted as a substitute.
MAX_LAUNCH_AGE_SECONDS = 3600
LAMPORT = Decimal("0.000000001")
INITIAL_SIM_NOW = 1_700_000_000

MODEL_LABEL = "ILLUSTRATIVE_NOT_A_GMGN_QUOTE"
DISCLAIMER_ACK = "ILLUSTRATIVE_PAPER_ONLY"
RESET_PHRASE = "RESET PAPER BOOK"
KILL_PHRASE = "KILL PAPER SESSION"

NOT_A_QUOTE = (
    "Configured illustrative fee model only. This is not an executable GMGN quote, "
    "not a chain fill, and not a prediction of profit."
)
GROSS_IS_NOT_NET = "A 20% gross price change is not a 20% net profit after costs."
NOT_SAFE = (
    "A synthetic screen pass is not a safety certification. "
    "Missing or unsupported checks are skips, never a default of safe."
)
EXITS_NOT_GUARANTEED = (
    "A loss trigger or a max-hold trigger only attempts a simulated sell. "
    "The attempt can fail or stay ambiguous, and the position then stays open."
)
