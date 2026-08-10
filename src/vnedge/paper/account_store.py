"""Cross-session paper account persistence.

Real processes restart. Without persistence, every restart silently resets
the paper account to starting equity and the trial's evidence becomes a
patchwork of fresh starts. This store snapshots the account after every
processed bar (atomic tmp+rename, same crash-safety as the Parquet store)
and restores it on the next launch.

Scope (v1, honest): balance, open positions, and the tracker's risk
counters (peak equity, daily baseline, loss streak). NOT restored: resting
limit orders (the paper path uses market orders only) and historical fill
lists (the journal is the durable record of those).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from vnedge.paper.simulated_exchange import PaperPosition, SimulatedExchange
from vnedge.runtime.portfolio_tracker import PortfolioTracker

logger = logging.getLogger(__name__)

# Restored balances above this multiple of starting equity are treated as
# corrupt (paper strategies here don't 10x an account between snapshots).
_MAX_BALANCE_MULTIPLE = 10.0
# Starting equity drift beyond this fraction means the store belongs to a
# differently-configured lane — refusing to resume beats resuming wrong.
_EQUITY_TOLERANCE = 0.01
# Snapshots older than this get a prominent warning (not a refusal — VMs
# sleep); the operator should sanity-check the resumed account.
_STALE_AFTER_DAYS = 7.0


class PaperAccountStore:
    def __init__(self, path: Path | str, trial_id: str) -> None:
        self.path = Path(path)
        self.trial_id = trial_id

    def save_from(
        self, exchange: SimulatedExchange, tracker: PortfolioTracker,
        *, plan: dict | None = None,
    ) -> None:
        state = {
            "trial_id": self.trial_id,
            "saved_at": datetime.now(UTC).isoformat(),
            "starting_equity": tracker.starting_equity_usd,
            "balance_usd": exchange.balance_usd,
            "positions": [
                {"symbol": p.symbol, "quantity": p.quantity, "entry_price": p.entry_price}
                for p in exchange.get_positions()
            ],
            "tracker": tracker.export_state(),
            # the active trade plan (stop/target) — without it a restart turns
            # an open position into an orphan requiring manual flatten
            "plan": plan,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.path)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        state = json.loads(self.path.read_text())
        if state.get("trial_id") != self.trial_id:
            raise ValueError(
                f"account state at {self.path} belongs to trial "
                f"'{state.get('trial_id')}', not '{self.trial_id}' — refusing to mix trials"
            )
        return state

    def restore_into(
        self, exchange: SimulatedExchange, tracker: PortfolioTracker,
        *, expected_symbol: str | None = None,
        expected_starting_equity: float | None = None,
    ) -> bool:
        """True if prior state existed and was restored.

        A moved or hand-edited store file could inject a wrong-symbol
        position or an absurd balance into a lane. When the caller supplies
        its configured `expected_symbol` / `expected_starting_equity`, the
        snapshot is validated against them and a mismatch raises ValueError
        (fail closed — refusing to resume beats resuming wrong).
        """
        state = self.load()
        if state is None:
            return False
        self._check_consistency(state, expected_symbol, expected_starting_equity)
        exchange.balance_usd = float(state["balance_usd"])
        exchange.positions = {
            p["symbol"]: PaperPosition(p["symbol"], float(p["quantity"]), float(p["entry_price"]))
            for p in state["positions"]
        }
        tracker.restore_state(state["tracker"])
        logger.info(
            "resumed paper account %s: balance $%.2f, %d position(s), loss streak %d",
            self.trial_id, exchange.balance_usd, len(exchange.positions),
            tracker.consecutive_losses,
        )
        return True

    def _check_consistency(
        self, state: dict, expected_symbol: str | None,
        expected_starting_equity: float | None,
    ) -> None:
        if expected_symbol is not None:
            for p in state["positions"]:
                if p["symbol"] != expected_symbol:
                    raise ValueError(
                        f"account state at {self.path} holds a position in "
                        f"'{p['symbol']}' but this lane trades "
                        f"'{expected_symbol}' — refusing to resume a "
                        f"wrong-symbol position"
                    )
        stored_equity = state.get("starting_equity")
        if expected_starting_equity is not None:
            if (
                stored_equity is None
                or abs(float(stored_equity) - expected_starting_equity)
                > _EQUITY_TOLERANCE * expected_starting_equity
            ):
                raise ValueError(
                    f"account state at {self.path} has starting_equity "
                    f"{stored_equity}, expected ~{expected_starting_equity} "
                    f"(±{_EQUITY_TOLERANCE:.0%}) — refusing to resume a "
                    f"differently-funded account"
                )
        # Balance sanity against the best-known starting equity: 0 < balance
        # <= 10x. NaN fails both comparisons, so it is rejected too.
        reference = (
            expected_starting_equity
            if expected_starting_equity is not None
            else (float(stored_equity) if stored_equity is not None else None)
        )
        balance = float(state["balance_usd"])
        if reference is not None and not (
            0 < balance <= _MAX_BALANCE_MULTIPLE * reference
        ):
            raise ValueError(
                f"account state at {self.path} has absurd balance "
                f"${balance} (starting equity ${reference}, allowed range "
                f"(0, {_MAX_BALANCE_MULTIPLE:.0f}x]) — refusing to resume"
            )
        saved_at = state.get("saved_at")
        if saved_at is not None:
            age = datetime.now(UTC) - datetime.fromisoformat(saved_at)
            age_days = age.total_seconds() / 86_400
            if age_days > _STALE_AFTER_DAYS:
                logger.warning(
                    "STALE ACCOUNT SNAPSHOT: %s was saved %.1f days ago "
                    "(> %.0f) — resuming anyway, but verify the account "
                    "matches reality before trusting this lane",
                    self.path, age_days, _STALE_AFTER_DAYS,
                )
