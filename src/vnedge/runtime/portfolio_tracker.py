"""Portfolio tracker — turns exchange truth into the AccountState the risk
gateway consumes.

Derives everything from the simulated exchange (positions, balance, fills)
plus its own bookkeeping: peak equity, UTC-day PnL baseline, and
consecutive-loss counting over completed round trips (position opened →
flat), with fees included in the round-trip verdict — a trade that "won"
less than its fees is a loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.risk_manager import AccountState


@dataclass
class _RoundTrip:
    realized_usd: float = 0.0
    fees_usd: float = 0.0


@dataclass
class PortfolioTracker:
    exchange: SimulatedExchange
    starting_equity_usd: float
    peak_equity_usd: float = field(init=False)
    consecutive_losses: int = field(default=0, init=False)
    _fills_seen: int = field(default=0, init=False)
    _day: date | None = field(default=None, init=False)
    _day_baseline: float = field(default=0.0, init=False)
    _open_round_trips: dict[str, _RoundTrip] = field(default_factory=dict, init=False)
    # net position per symbol derived incrementally from fills — judging a
    # historical fill against CURRENT exchange positions is wrong when a
    # round trip opened and closed within one processing batch
    _net_qty: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.peak_equity_usd = self.starting_equity_usd
        self._day_baseline = self.starting_equity_usd

    # --- Valuation --------------------------------------------------------------
    def unrealized_pnl_usd(self) -> float:
        total = 0.0
        for pos in self.exchange.get_positions():
            quote = self.exchange.quotes.get(pos.symbol)
            if quote is None:
                continue  # no quote yet (e.g. just resumed): mark at entry
            mark = (quote[0] + quote[1]) / 2.0
            total += pos.quantity * (mark - pos.entry_price)
        return total

    def equity_usd(self) -> float:
        return self.exchange.get_balances()["USDT"] + self.unrealized_pnl_usd()

    # --- Per-bar update -----------------------------------------------------------
    def on_bar(self, ts: datetime) -> None:
        """Call once per bar AFTER quotes are updated. Order matters: process
        fills first, then roll the day, then update the peak."""
        self._process_new_fills()
        bar_day = ts.date()
        if self._day is None:
            self._day = bar_day
        elif bar_day != self._day:
            self._day = bar_day
            self._day_baseline = self.equity_usd()  # daily PnL resets at UTC midnight
        self.peak_equity_usd = max(self.peak_equity_usd, self.equity_usd())

    def _process_new_fills(self) -> None:
        fills = self.exchange.get_fills()
        for fill in fills[self._fills_seen:]:
            rt = self._open_round_trips.setdefault(fill.symbol, _RoundTrip())
            rt.realized_usd += fill.realized_pnl_usd
            rt.fees_usd += fill.fee_usd
            signed = fill.quantity if fill.buy else -fill.quantity
            net = self._net_qty.get(fill.symbol, 0.0) + signed
            self._net_qty[fill.symbol] = net
            if abs(net) < 1e-12:  # round trip completed at THIS fill
                pnl = rt.realized_usd - rt.fees_usd
                if pnl < 0:
                    self.consecutive_losses += 1
                else:
                    self.consecutive_losses = 0
                del self._open_round_trips[fill.symbol]
                del self._net_qty[fill.symbol]
        self._fills_seen = len(fills)

    # --- Persistence (cross-session resume) ---------------------------------------
    def export_state(self) -> dict:
        return {
            "peak_equity_usd": self.peak_equity_usd,
            "consecutive_losses": self.consecutive_losses,
            "day": self._day.isoformat() if self._day else None,
            "day_baseline": self._day_baseline,
        }

    def restore_state(self, state: dict) -> None:
        """Restore risk counters after a restart. Fill-derived internals are
        rebuilt from the exchange's CURRENT positions: the new session's fill
        list starts empty, so net quantities must be seeded from the restored
        positions or round-trip loss counting breaks on the next close."""
        self.peak_equity_usd = float(state["peak_equity_usd"])
        self.consecutive_losses = int(state["consecutive_losses"])
        self._day = date.fromisoformat(state["day"]) if state["day"] else None
        self._day_baseline = float(state["day_baseline"])
        self._fills_seen = len(self.exchange.get_fills())
        self._net_qty = {
            p.symbol: p.quantity for p in self.exchange.get_positions()
        }
        self._open_round_trips = {
            p.symbol: _RoundTrip() for p in self.exchange.get_positions()
        }

    # --- Gateway feed -----------------------------------------------------------------
    def account_state(self) -> AccountState:
        exposure: dict[str, float] = {}
        for pos in self.exchange.get_positions():
            # no quote yet (just-resumed session): expose at entry price so the
            # gateway still sees honest notional instead of the lane crashing
            quote = self.exchange.quotes.get(pos.symbol)
            mark = (quote[0] + quote[1]) / 2.0 if quote else pos.entry_price
            exposure[pos.symbol] = abs(pos.quantity) * mark
        equity = self.equity_usd()
        return AccountState(
            equity_usd=equity,
            daily_pnl_usd=equity - self._day_baseline,
            peak_equity_usd=self.peak_equity_usd,
            open_positions=len(self.exchange.get_positions()),
            exposure_by_symbol_usd=exposure,
            total_exposure_usd=sum(exposure.values()),
            consecutive_losses=self.consecutive_losses,
        )
