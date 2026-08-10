"""Strategy interface for the backtester (and later paper/live engines).

Contract that keeps lookahead bias structurally impossible:

- ``prepare()`` may use vectorized operations over the whole frame, but only
  causal ones (rolling windows, shifts backward). It must not leak future
  rows into earlier rows.
- ``signal(df, index)`` is called at the CLOSE of bar ``index`` and may read
  rows ``0..index`` only. The engine fills any resulting intent at the OPEN
  of bar ``index + 1`` — a strategy never trades on information from the bar
  it trades in.
- Every intent must carry a stop price. Stop-less strategies are not
  representable in this system by design.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

import pandas as pd


@dataclass(frozen=True)
class SignalIntent:
    side: Literal["long", "short"]
    stop_price: float
    take_profit_price: float | None = None
    #: Optional TP ladder (tp1, tp2, tp3 …). Strategies that emit a ladder use
    #: the runtime active-exit policy: partial at TP1/TP2, fee-aware breakeven
    #: after TP1, and final close on the last level. Strategies without a
    #: ladder keep the classic single ``take_profit_price`` full-close behavior.
    take_profit_levels: tuple[float, ...] = ()
    reason: str = ""  # human-readable trigger explanation — explainability is a feature

    def __post_init__(self) -> None:
        if self.stop_price <= 0:
            raise ValueError("stop_price must be positive — stop-less intents are forbidden")


@dataclass(frozen=True)
class StrategyExitIntent:
    """Strategy-managed close request for an already-open position.

    This is intentionally narrower than ``SignalIntent``: it can only close an
    existing position, never open or resize one. Runners still submit the actual
    reduce-only order through their normal gateway/journal/order-manager path.
    """

    reason: str
    exit_price: float | None = None

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("exit reason must be non-empty")
        if self.exit_price is not None and self.exit_price <= 0:
            raise ValueError("exit_price must be positive when supplied")


class BaseStrategy(ABC):
    """Subclass and implement prepare() + signal()."""

    #: bars required before signal() is first called (indicator warmup)
    warmup_bars: int = 0
    strategy_id: str = "unnamed"

    @abstractmethod
    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of ``candles`` with indicator columns added.
        Must not mutate the input frame."""

    @abstractmethod
    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        """Entry decision at the close of bar ``index``. Read rows <= index only."""

    def exit_signal(
        self,
        df: pd.DataFrame,
        index: int,
        side: str,
        entry_price: float,
    ) -> StrategyExitIntent | None:
        """Optional strategy-managed exit at the close of bar ``index``.

        Implementations may read only rows <= ``index``. Stop/TP capital
        protection remains runner-owned and is evaluated before this hook.
        Returning an intent asks the runner to close reduce-only at bar close.
        """
        return None

    def synthesize_exit_plan(
        self, df: pd.DataFrame, index: int, side: str, entry_price: float
    ) -> SignalIntent | None:
        """Rebuild a protective exit plan for a restored position whose
        original plan was lost (legacy account snapshots predating plan
        persistence). Return None (default) if the strategy cannot do this
        honestly — the runner then falls back to the orphan guard (entries
        halted, manual flatten). Implementations must use the SAME formulas
        as signal() so the rebuilt stop/target match an uninterrupted run as
        closely as the data allows."""
        return None
