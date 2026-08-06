"""Causal closed-candle replay for the Delta scalper signal generator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import fmean

from vnedge.scalping.delta_engine.candle_store import (
    ClosedCandleAggregator,
    MultiTimeframeCandleStore,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.signal_generator import DeltaScalperSignalGenerator
from vnedge.scalping.delta_engine.types import Candle, Side, SignalCandidate


@dataclass(frozen=True)
class BacktestTrade:
    scanner_id: str
    symbol: str
    side: Side
    decision_ts: datetime
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    exit_reason: str
    hold_seconds: int
    gross_bps: float
    cost_bps: float
    net_bps: float
    mfe_bps: float
    mae_bps: float
    scalper_compliant: bool
    planned_stop_bps: float
    planned_target_bps: float
    same_bar_ambiguous: bool
    regime_at_entry: str
    trend_regime_at_entry: str
    trend_direction_at_entry: str
    volatility_regime_at_entry: str
    session_regime_at_entry: str
    regime_shift_at_entry: bool
    change_point_window_at_entry: str
    change_point_bars_since_shift: int | None
    change_point_return_score: float
    change_point_volatility_score: float
    expected_move_bps: float
    expected_net_bps: float
    scalper_probability: float
    confidence: float
    deto_enabled: bool
    entry_is_maker: bool

    def to_dict(self) -> dict[str, object]:
        row = self.__dict__.copy()
        row["side"] = self.side.value
        for key in ("decision_ts", "entry_ts", "exit_ts"):
            row[key] = row[key].isoformat()
        return row


@dataclass(frozen=True)
class BacktestReport:
    symbol: str
    started_at: datetime | None
    ended_at: datetime | None
    bars: int
    trades: tuple[BacktestTrade, ...]
    net_bps: float
    average_net_bps: float
    win_rate: float
    profit_factor: float
    max_drawdown_bps: float
    trades_per_day: float
    scalper_compliance_rate: float
    missing_one_minute_bars: int
    unresolved_trades_dropped: int
    data_quality_pass: bool
    same_bar_ambiguity_rate: float
    research_only: bool = True
    can_trade: bool = False
    can_promote: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "bars": self.bars,
            "trades": [trade.to_dict() for trade in self.trades],
            "summary": {
                "trade_count": len(self.trades),
                "net_bps": self.net_bps,
                "average_net_bps": self.average_net_bps,
                "win_rate": self.win_rate,
                "profit_factor": self.profit_factor,
                "max_drawdown_bps": self.max_drawdown_bps,
                "trades_per_day": self.trades_per_day,
                "scalper_compliance_rate": self.scalper_compliance_rate,
                "missing_one_minute_bars": self.missing_one_minute_bars,
                "unresolved_trades_dropped": self.unresolved_trades_dropped,
                "data_quality_pass": self.data_quality_pass,
                "same_bar_ambiguity_rate": self.same_bar_ambiguity_rate,
            },
            "policy": {
                "research_only": True,
                "can_trade": False,
                "can_promote": False,
                "entry_fill": "next_1m_open",
                "same_bar_stop_and_target": "stop_first_conservative",
                "l2_role": "confirmation_only_not_replayed",
            },
        }


@dataclass
class OpenBacktestTrade:
    candidate: SignalCandidate
    entry_bar: Candle
    mfe_bps: float = 0.0
    mae_bps: float = 0.0


class CausalScalperBacktester:
    """Next-open entries and conservative intrabar exit resolution."""

    def __init__(
        self,
        generator: DeltaScalperSignalGenerator,
        fee_model: DeltaFeeModel,
        store: MultiTimeframeCandleStore,
    ) -> None:
        self.generator = generator
        self.fee_model = fee_model
        self.store = store

    def run(self, symbol: str, candles_1m: list[Candle]) -> BacktestReport:
        native = symbol.upper()
        rows = sorted(candles_1m, key=lambda row: row.ts)
        if any(row.tf != "1m" for row in rows):
            raise ValueError("backtester accepts closed 1m candles only")
        aggregator = ClosedCandleAggregator()
        pending: SignalCandidate | None = None
        open_trade: OpenBacktestTrade | None = None
        trades: list[BacktestTrade] = []
        previous_ts: datetime | None = None
        missing_bars = 0
        unresolved_dropped = 0
        for bar in rows:
            if previous_ts is not None and bar.ts <= previous_ts:
                raise ValueError("1m candles must have unique ascending timestamps")
            if previous_ts is not None and bar.ts - previous_ts != timedelta(minutes=1):
                missing_bars += max(
                    1, int((bar.ts - previous_ts).total_seconds() // 60) - 1
                )
                unresolved_dropped += int(open_trade is not None)
                pending = None
                open_trade = None
                self.store.reset_symbol(native)
                aggregator = ClosedCandleAggregator()
            previous_ts = bar.ts
            # A signal decided at t may only enter at the next bar's open.
            if pending is not None and open_trade is None:
                entry_bar = Candle(
                    ts=bar.ts,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    tf=bar.tf,
                )
                open_trade = OpenBacktestTrade(pending, entry_bar)
                pending = None
            if open_trade is not None:
                resolved = self.resolve_on_bar(open_trade, bar)
                if resolved is not None:
                    trades.append(resolved)
                    open_trade = None
            self.store.append_closed(native, bar, observed_at=bar.ts)
            for higher in aggregator.on_one_minute(native, bar):
                self.store.append_closed(native, higher, observed_at=bar.ts)
            decision = self.generator.on_candle_closed(native, "1m", now=bar.ts)
            if decision.selected is not None and pending is None and open_trade is None:
                pending = decision.selected
        unresolved_dropped += int(open_trade is not None)
        return self._report(
            native,
            rows,
            trades,
            missing_bars=missing_bars,
            unresolved_dropped=unresolved_dropped,
        )

    def resolve_on_bar(
        self,
        active: OpenBacktestTrade,
        bar: Candle,
    ) -> BacktestTrade | None:
        candidate = active.candidate
        entry_bar = active.entry_bar
        entry = entry_bar.open
        elapsed = int((bar.ts - entry_bar.ts).total_seconds()) + 60
        if candidate.side is Side.LONG:
            stop_distance_bps = (1 - candidate.stop_loss / candidate.entry_price) * 10_000
            target_distance_bps = (
                candidate.take_profits[0] / candidate.entry_price - 1
            ) * 10_000
            stop_price = entry * (1 - stop_distance_bps / 10_000)
            first_target = entry * (1 + target_distance_bps / 10_000)
        else:
            stop_distance_bps = (candidate.stop_loss / candidate.entry_price - 1) * 10_000
            target_distance_bps = (
                1 - candidate.take_profits[0] / candidate.entry_price
            ) * 10_000
            stop_price = entry * (1 + stop_distance_bps / 10_000)
            first_target = entry * (1 - target_distance_bps / 10_000)
        if candidate.side is Side.LONG:
            stop_hit = bar.low <= stop_price
            target_hit = bar.high >= first_target
            gross = (lambda price: (price / entry - 1) * 10_000)
            mfe = gross(bar.high)
            mae = max(0.0, -gross(bar.low))
        else:
            stop_hit = bar.high >= stop_price
            target_hit = bar.low <= first_target
            gross = (lambda price: (entry / price - 1) * 10_000)
            mfe = gross(bar.low)
            mae = max(0.0, -gross(bar.high))
        active.mfe_bps = max(active.mfe_bps, max(0.0, mfe))
        active.mae_bps = max(active.mae_bps, mae)
        if stop_hit:  # conservative when stop and target touch in the same candle
            exit_price, reason = stop_price, "stop"
        elif target_hit:
            exit_price, reason = first_target, "target_1"
        elif elapsed >= candidate.time_stop_seconds:
            exit_price, reason = bar.close, "time_stop"
        else:
            return None
        gross_bps = gross(exit_price)
        costs = self.fee_model.breakdown(
            candidate.symbol,
            entry_is_maker=candidate.entry_is_maker,
            hold_seconds=elapsed,
        )
        raw_profile = candidate.metadata.get("regime_profile")
        profile = raw_profile if isinstance(raw_profile, dict) else {}
        raw_change_point = profile.get("change_point")
        change_point = raw_change_point if isinstance(raw_change_point, dict) else {}
        raw_bars_since_shift = change_point.get("bars_since_shift")
        return BacktestTrade(
            scanner_id=candidate.scanner_id,
            symbol=candidate.symbol,
            side=candidate.side,
            decision_ts=candidate.decision_ts,
            entry_ts=entry_bar.ts,
            exit_ts=bar.ts,
            entry_price=entry,
            exit_price=exit_price,
            exit_reason=reason,
            hold_seconds=elapsed,
            gross_bps=gross_bps,
            cost_bps=costs.total_bps,
            net_bps=gross_bps - costs.total_bps,
            mfe_bps=active.mfe_bps,
            mae_bps=active.mae_bps,
            scalper_compliant=costs.scalper_eligible,
            planned_stop_bps=stop_distance_bps,
            planned_target_bps=target_distance_bps,
            same_bar_ambiguous=stop_hit and target_hit,
            regime_at_entry=str(candidate.metadata.get("regime") or "unknown"),
            trend_regime_at_entry=str(profile.get("trend") or "unknown"),
            trend_direction_at_entry=str(
                profile.get("trend_direction") or "unknown"
            ),
            volatility_regime_at_entry=str(profile.get("volatility") or "unknown"),
            session_regime_at_entry=str(profile.get("session") or "unknown"),
            regime_shift_at_entry=bool(change_point.get("regime_shift")),
            change_point_window_at_entry=str(
                change_point.get("shift_window") or "unavailable"
            ),
            change_point_bars_since_shift=(
                int(raw_bars_since_shift)
                if isinstance(raw_bars_since_shift, (int, float))
                else None
            ),
            change_point_return_score=float(
                change_point.get("return_score") or 0.0
            ),
            change_point_volatility_score=float(
                change_point.get("volatility_score") or 0.0
            ),
            expected_move_bps=candidate.expected_move_bps,
            expected_net_bps=candidate.fee_adjusted_expectancy_bps,
            scalper_probability=candidate.scalper_probability,
            confidence=candidate.confidence,
            deto_enabled=costs.deto_enabled,
            entry_is_maker=candidate.entry_is_maker,
        )

    @staticmethod
    def _report(
        symbol: str,
        rows: list[Candle],
        trades: list[BacktestTrade],
        *,
        missing_bars: int,
        unresolved_dropped: int,
    ) -> BacktestReport:
        wins = [trade.net_bps for trade in trades if trade.net_bps > 0]
        losses = [-trade.net_bps for trade in trades if trade.net_bps < 0]
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for trade in trades:
            equity += trade.net_bps
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        days = (
            max(1.0, (rows[-1].ts - rows[0].ts).total_seconds() / 86_400.0)
            if len(rows) >= 2
            else 1.0
        )
        return BacktestReport(
            symbol=symbol,
            started_at=rows[0].ts if rows else None,
            ended_at=rows[-1].ts if rows else None,
            bars=len(rows),
            trades=tuple(trades),
            net_bps=sum(trade.net_bps for trade in trades),
            average_net_bps=fmean(trade.net_bps for trade in trades) if trades else 0.0,
            win_rate=len(wins) / len(trades) if trades else 0.0,
            profit_factor=sum(wins) / sum(losses) if losses else (float("inf") if wins else 0.0),
            max_drawdown_bps=max_drawdown,
            trades_per_day=len(trades) / days,
            scalper_compliance_rate=(
                sum(trade.scalper_compliant for trade in trades) / len(trades) if trades else 0.0
            ),
            missing_one_minute_bars=missing_bars,
            unresolved_trades_dropped=unresolved_dropped,
            data_quality_pass=missing_bars == 0 and unresolved_dropped == 0,
            same_bar_ambiguity_rate=(
                sum(trade.same_bar_ambiguous for trade in trades) / len(trades)
                if trades
                else 0.0
            ),
        )
