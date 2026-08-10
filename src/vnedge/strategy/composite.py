"""Composite strategy that arbitrates multiple child signals.

This adapter lets the runtime keep its single-signal contract while research
lanes run multiple hypotheses for the same symbol. The selected signal still
flows through sizing, journaling, the risk gateway, and the order manager.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

import pandas as pd

from vnedge.execution.signal_arbiter import (
    ArbitrationDecision,
    SignalArbiter,
    SignalCandidate,
)
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent


class CompositeSignalStrategy(BaseStrategy):
    """Run child strategies and return the arbiter's winning signal."""

    strategy_id = "signal_arbiter_v1"

    def __init__(
        self,
        strategies: Sequence[BaseStrategy],
        arbiter: SignalArbiter,
        *,
        symbol: str,
        candidate_defaults: Mapping[str, Mapping[str, Any]] | None = None,
        strategy_id: str = "signal_arbiter_v1",
    ) -> None:
        if not strategies:
            raise ValueError("CompositeSignalStrategy requires at least one child strategy")
        self.strategies = tuple(strategies)
        self.arbiter = arbiter
        self.symbol = symbol
        self.candidate_defaults = dict(candidate_defaults or {})
        self.strategy_id = strategy_id
        self.warmup_bars = max(strategy.warmup_bars for strategy in self.strategies)
        self.last_decision = ArbitrationDecision((), ())
        self._prepared: dict[str, pd.DataFrame] = {}

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        self._prepared = {}
        for index, strategy in enumerate(self.strategies):
            source_id = self._source_id(strategy, index)
            self._prepared[source_id] = strategy.prepare(candles)
        return candles.copy()

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        candidates: list[SignalCandidate] = []
        for child_index, strategy in enumerate(self.strategies):
            if index < strategy.warmup_bars:
                continue
            source_id = self._source_id(strategy, child_index)
            child_df = self._prepared.get(source_id)
            if child_df is None:
                child_df = strategy.prepare(df)
                self._prepared[source_id] = child_df
            signal = strategy.signal(child_df, index)
            if signal is None:
                continue
            candidates.append(self._candidate(strategy, source_id, signal))

        self.last_decision = self.arbiter.arbitrate(candidates)
        return self.last_decision.to_signal()

    def _source_id(self, strategy: BaseStrategy, index: int) -> str:
        configured = self.candidate_defaults.get(f"{strategy.strategy_id}#{index + 1}", {})
        source = configured.get("source_id")
        if source is not None:
            return str(source)
        return f"{strategy.strategy_id}#{index + 1}"

    def _candidate(
        self,
        strategy: BaseStrategy,
        source_id: str,
        signal: SignalIntent,
    ) -> SignalCandidate:
        defaults = self._defaults_for(strategy.strategy_id, source_id)
        metadata = defaults.get("metadata", {})
        return SignalCandidate(
            source_id=source_id,
            strategy_id=strategy.strategy_id,
            symbol=self.symbol,
            signal=signal,
            expected_edge_bps=float(defaults.get("expected_edge_bps", 0.0)),
            expected_cost_bps=float(defaults.get("expected_cost_bps", 0.0)),
            profit_factor=self._optional_float(defaults.get("profit_factor")),
            confidence=float(defaults.get("confidence", 1.0)),
            route=cast(Any, defaults.get("route", "UNKNOWN")),
            planned_notional_usd=self._optional_float(defaults.get("planned_notional_usd")),
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        )

    def _defaults_for(self, strategy_id: str, source_id: str) -> Mapping[str, Any]:
        merged: dict[str, Any] = {}
        merged.update(self.candidate_defaults.get(strategy_id, {}))
        merged.update(self.candidate_defaults.get(source_id, {}))
        return merged

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value is None:
            return None
        return float(value)
