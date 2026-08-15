"""Selection-only study of earlier range-compression breakout entries.

This module deliberately keeps four variants separate:

* ``close_5m`` waits for the complete breakout candle and enters next minute.
* ``close_1m_volume`` uses a complete 1m breakout/volume candle and enters next minute.
* ``boundary_stop`` models a resting stop at the already-known range boundary.
* ``same_signal_boundary_oracle`` retroactively selects only breakouts which later
  pass the 5m close filters.  It diagnoses entry delay but is not deployable.

Only the already-open selection window is processed.  The sealed tail is never
loaded into a simulator or scored by this study.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean, median, pstdev
from tempfile import NamedTemporaryFile
from types import MappingProxyType

from vnedge.research.delta_scalper_backtest import _load_candles
from vnedge.scalping.delta_engine.backtester import (
    BacktestTrade,
    CausalScalperBacktester,
    OpenBacktestTrade,
)
from vnedge.scalping.delta_engine.candle_store import (
    ClosedCandleAggregator,
    MultiTimeframeCandleStore,
)
from vnedge.scalping.delta_engine.change_point import CausalCusumDetector
from vnedge.scalping.delta_engine.range_compression import (
    RangeCompressionConfig,
    load_range_compression_config,
    range_compression_fee_model,
)
from vnedge.scalping.delta_engine.regime import _true_ranges
from vnedge.scalping.delta_engine.types import Candle, ChangePointProfile, Side, SignalCandidate

DEFAULT_CONFIG = Path("configs/research/range_compression_breakout_v1.yaml")
DEFAULT_CACHE = Path("data/delta_scalper_cache")
DEFAULT_V1_RESULT = Path(
    "research/live_research/range_compression_breakout_v1_latest.json"
)
DEFAULT_OUTPUT = Path(
    "research/live_research/range_compression_early_entry_study_latest.json"
)


@dataclass(frozen=True)
class ArmedRange:
    symbol: str
    locked_ts: datetime
    high: float
    low: float
    atr_bps: float
    five_minute_volume_median: float
    five_minute_atr: float
    change_point: ChangePointProfile


@dataclass
class StudyPosition:
    active: OpenBacktestTrade
    locked_ts: datetime
    entry_mode: str
    time_to_mfe_seconds: int = 0


@dataclass
class ModeState:
    mode: str
    pending: tuple[SignalCandidate, datetime] | None = None
    position: StudyPosition | None = None
    last_fire_ts: datetime | None = None
    trades: list[tuple[BacktestTrade, StudyPosition]] = field(default_factory=list)


@dataclass(frozen=True)
class OracleRequest:
    symbol: str
    candidate: SignalCandidate
    locked_ts: datetime
    entry_index: int
    entry_price: float


@dataclass(frozen=True)
class SymbolStudy:
    symbol: str
    source_bars: int
    missing_minutes: int
    unresolved_dropped: int
    modes: dict[str, tuple[tuple[BacktestTrade, StudyPosition], ...]]


class CompressionArmState:
    """Past-only reproduction of the frozen v1 compression definition."""

    def __init__(self, config: RangeCompressionConfig) -> None:
        self.config = config
        self.closes: deque[float] = deque(
            maxlen=config.compression.bollinger_window_bars
        )
        self.widths: deque[float] = deque(
            maxlen=config.compression.percentile_history_bars
        )
        self.flags: deque[bool] = deque(
            maxlen=config.compression.compression_observation_bars
        )

    def reset(self) -> None:
        self.closes.clear()
        self.widths.clear()
        self.flags.clear()

    def update(self, row: Candle) -> bool:
        settings = self.config.compression
        self.closes.append(row.close)
        compressed = False
        if len(self.closes) == settings.bollinger_window_bars:
            mean = fmean(self.closes)
            width = 4.0 * pstdev(self.closes) / mean if mean else 0.0
            self.widths.append(width)
            if len(self.widths) == settings.percentile_history_bars:
                rank = (
                    sum(value < width for value in self.widths)
                    + 0.5 * sum(value == width for value in self.widths)
                ) / len(self.widths)
                compressed = rank <= settings.maximum_width_percentile
        self.flags.append(compressed)
        return (
            len(self.flags) == settings.compression_observation_bars
            and sum(self.flags) >= settings.minimum_compressed_bars
            and (not settings.require_immediately_prior_compression or self.flags[-1])
        )


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _body_ratio(row: Candle) -> float:
    span = max(row.high - row.low, row.close * 1e-9)
    return abs(row.close - row.open) / span


def _price_at_bps(price: float, side: Side, bps: float) -> float:
    direction = 1.0 if side is Side.LONG else -1.0
    return price * (1.0 + direction * bps / 10_000.0)


def _threshold(arm: ArmedRange, side: Side, config: RangeCompressionConfig) -> float:
    bps = config.breakout.minimum_close_beyond_range_bps
    base = arm.high if side is Side.LONG else arm.low
    return _price_at_bps(base, side, bps)


def _build_candidate(
    *,
    mode: str,
    arm: ArmedRange,
    side: Side,
    decision_ts: datetime,
    reference_price: float,
    config: RangeCompressionConfig,
    cost_bps: float,
    trigger: dict[str, object],
) -> SignalCandidate:
    # A tight initial stop just inside the locked boundary.  This is known at
    # setup time; no decision-bar extreme or future ATR is used.
    stop_bps = max(
        config.exit.min_stop_bps,
        config.exit.structural_buffer_bps + 0.20 * arm.atr_bps,
    )
    stop_bps = min(stop_bps, config.exit.max_stop_bps)
    target_bps = max(
        stop_bps * config.exit.reward_risk,
        cost_bps * config.exit.minimum_target_cost_multiple,
    )
    probability = config.structural_prior.probability
    raw_expectancy = probability * target_bps - (1.0 - probability) * stop_bps
    return SignalCandidate(
        scanner_id=f"range_compression_early_entry_study:{mode}",
        symbol=arm.symbol,
        side=side,
        decision_ts=decision_ts,
        entry_price=reference_price,
        stop_loss=_price_at_bps(reference_price, side, -stop_bps),
        take_profits=(_price_at_bps(reference_price, side, target_bps),),
        time_stop_seconds=config.exit.time_stop_seconds,
        expected_hold_seconds=config.exit.expected_hold_seconds,
        expected_move_bps=target_bps,
        raw_expectancy_bps=raw_expectancy,
        modeled_cost_bps=cost_bps,
        fee_adjusted_expectancy_bps=raw_expectancy - cost_bps,
        scalper_probability=probability,
        confidence=config.structural_prior.confidence,
        entry_is_maker=False,
        metadata=MappingProxyType(
            {
                "signal_type": "range_compression_breakout_early_entry_study",
                "entry_mode": mode,
                "locked_ts": arm.locked_ts.isoformat(),
                "range_high": arm.high,
                "range_low": arm.low,
                "atr_bps_at_lock": arm.atr_bps,
                "trigger": trigger,
                "research_only": True,
            }
        ),
    )


def _favorable_bps(position: OpenBacktestTrade, bar: Candle) -> float:
    entry = position.entry_bar.open
    if position.candidate.side is Side.LONG:
        return max(0.0, (bar.high / entry - 1.0) * 10_000.0)
    return max(0.0, (entry / bar.low - 1.0) * 10_000.0)


def _advance_mode(
    state: ModeState,
    bar: Candle,
    resolver: CausalScalperBacktester,
) -> None:
    if state.pending is not None and state.position is None:
        candidate, locked_ts = state.pending
        state.position = StudyPosition(
            OpenBacktestTrade(candidate, bar), locked_ts, state.mode
        )
        state.pending = None
    if state.position is None:
        return
    position = state.position
    before = position.active.mfe_bps
    favorable = _favorable_bps(position.active, bar)
    if favorable > before:
        position.time_to_mfe_seconds = max(
            0, int((bar.ts - position.active.entry_bar.ts).total_seconds()) + 60
        )
    resolved = resolver.resolve_on_bar(position.active, bar)
    if resolved is not None:
        state.trades.append((resolved, position))
        state.position = None


def _eligible(state: ModeState, ts: datetime, config: RangeCompressionConfig) -> bool:
    return (
        state.pending is None
        and state.position is None
        and (
            state.last_fire_ts is None
            or ts - state.last_fire_ts
            >= timedelta(minutes=config.breakout.cooldown_minutes)
        )
    )


def _cross_side(bar: Candle, arm: ArmedRange, config: RangeCompressionConfig) -> Side | None:
    up = bar.high >= _threshold(arm, Side.LONG, config)
    down = bar.low <= _threshold(arm, Side.SHORT, config)
    if up == down:  # neither or an unknowable two-sided intrabar path
        return None
    return Side.LONG if up else Side.SHORT


def _close_side(bar: Candle, arm: ArmedRange, config: RangeCompressionConfig) -> Side | None:
    if bar.close >= _threshold(arm, Side.LONG, config):
        return Side.LONG
    if bar.close <= _threshold(arm, Side.SHORT, config):
        return Side.SHORT
    return None


def _arm_from_history(
    symbol: str,
    history: deque[Candle],
    arm_state: CompressionArmState,
    change: ChangePointProfile,
    config: RangeCompressionConfig,
) -> ArmedRange | None:
    latest = history[-1]
    if not arm_state.update(latest):
        return None
    breakout = config.breakout
    if breakout.require_recent_cusum_shift and (
        not change.detector_ready
        or change.bars_since_shift is None
        or change.bars_since_shift > breakout.maximum_bars_since_cusum_shift
    ):
        return None
    compression = config.compression
    if len(history) < max(
        compression.range_lookback_bars,
        breakout.volume_history_bars,
        breakout.atr_window_bars + 1,
    ):
        return None
    range_rows = list(history)[-compression.range_lookback_bars :]
    volume_rows = list(history)[-breakout.volume_history_bars :]
    atr = fmean(_true_ranges(tuple(history))[-breakout.atr_window_bars :])
    return ArmedRange(
        symbol=symbol,
        locked_ts=latest.ts,
        high=max(row.high for row in range_rows),
        low=min(row.low for row in range_rows),
        atr_bps=atr / latest.close * 10_000.0 if latest.close else 0.0,
        five_minute_volume_median=median(row.volume for row in volume_rows),
        five_minute_atr=atr,
        change_point=change,
    )


def _simulate_oracles(
    rows: list[Candle],
    requests: list[OracleRequest],
    resolver: CausalScalperBacktester,
) -> tuple[tuple[BacktestTrade, StudyPosition], ...]:
    results: list[tuple[BacktestTrade, StudyPosition]] = []
    for request in requests:
        source = rows[request.entry_index]
        entry_bar = Candle(
            ts=source.ts,
            open=request.entry_price,
            high=max(source.high, request.entry_price),
            low=min(source.low, request.entry_price),
            close=source.close,
            volume=source.volume,
            tf="1m",
        )
        position = StudyPosition(
            OpenBacktestTrade(request.candidate, entry_bar),
            request.locked_ts,
            "same_signal_boundary_oracle",
        )
        for bar in rows[request.entry_index :]:
            before = position.active.mfe_bps
            favorable = _favorable_bps(position.active, bar)
            if favorable > before:
                position.time_to_mfe_seconds = max(
                    0, int((bar.ts - entry_bar.ts).total_seconds()) + 60
                )
            resolved = resolver.resolve_on_bar(position.active, bar)
            if resolved is not None:
                results.append((resolved, position))
                break
    return tuple(results)


def simulate_symbol(
    symbol: str,
    input_rows: list[Candle],
    config: RangeCompressionConfig,
    *,
    selection_end: datetime,
    process_end: datetime,
) -> SymbolStudy:
    rows = [row for row in sorted(input_rows, key=lambda item: item.ts) if row.ts <= process_end]
    native = symbol.upper()
    fee_model = range_compression_fee_model(config)
    cost_bps = fee_model.breakdown(
        native,
        entry_is_maker=False,
        hold_seconds=config.exit.expected_hold_seconds,
    ).total_bps
    resolver = CausalScalperBacktester(
        None,  # type: ignore[arg-type] -- canonical conservative resolver only
        fee_model,
        MultiTimeframeCandleStore(),
    )
    modes = {
        name: ModeState(name)
        for name in ("close_5m", "close_1m_volume", "boundary_stop")
    }
    aggregator = ClosedCandleAggregator(("5m",))
    history: deque[Candle] = deque(maxlen=320)
    detector = CausalCusumDetector(config.cusum)
    arm_state = CompressionArmState(config)
    arm: ArmedRange | None = None
    previous_ts: datetime | None = None
    missing = 0
    dropped = 0
    one_minute_history: deque[Candle] = deque(maxlen=30)
    current_bucket: list[tuple[int, Candle]] = []
    oracle_requests: list[OracleRequest] = []

    for index, bar in enumerate(rows):
        if bar.tf != "1m":
            raise ValueError("early-entry study accepts closed 1m candles only")
        if previous_ts is not None:
            if bar.ts <= previous_ts:
                raise ValueError("source candles must be unique and ascending")
            gap = max(0, int((bar.ts - previous_ts).total_seconds() // 60) - 1)
            if gap:
                missing += gap
                dropped += sum(
                    int(state.pending is not None) + int(state.position is not None)
                    for state in modes.values()
                )
                for state in modes.values():
                    state.pending = None
                    state.position = None
                aggregator = ClosedCandleAggregator(("5m",))
                history.clear()
                one_minute_history.clear()
                current_bucket.clear()
                detector = CausalCusumDetector(config.cusum)
                arm_state.reset()
                arm = None
        previous_ts = bar.ts

        for state in modes.values():
            _advance_mode(state, bar, resolver)

        scoring = bar.ts <= selection_end
        if scoring and arm is not None:
            boundary = modes["boundary_stop"]
            if _eligible(boundary, bar.ts, config):
                side = _cross_side(bar, arm, config)
                if side is not None:
                    threshold = _threshold(arm, side, config)
                    fill = (
                        max(threshold, bar.open)
                        if side is Side.LONG
                        else min(threshold, bar.open)
                    )
                    candidate = _build_candidate(
                        mode=boundary.mode,
                        arm=arm,
                        side=side,
                        decision_ts=bar.ts,
                        reference_price=fill,
                        config=config,
                        cost_bps=cost_bps,
                        trigger={"kind": "resting_stop_1m_ohlc_proxy"},
                    )
                    entry_bar = Candle(
                        ts=bar.ts,
                        open=fill,
                        high=max(bar.high, fill),
                        low=min(bar.low, fill),
                        close=bar.close,
                        volume=bar.volume,
                        tf="1m",
                    )
                    boundary.position = StudyPosition(
                        OpenBacktestTrade(candidate, entry_bar),
                        arm.locked_ts,
                        boundary.mode,
                    )
                    boundary.last_fire_ts = bar.ts
                    _advance_mode(boundary, bar, resolver)

            minute = modes["close_1m_volume"]
            if _eligible(minute, bar.ts, config) and len(one_minute_history) >= 20:
                side = _close_side(bar, arm, config)
                baseline_volume = median(row.volume for row in list(one_minute_history)[-20:])
                relative_volume = bar.volume / baseline_volume if baseline_volume > 0 else 0.0
                if (
                    side is not None
                    and _body_ratio(bar) >= config.breakout.minimum_body_ratio
                    and relative_volume >= config.breakout.minimum_relative_volume
                ):
                    minute.pending = (
                        _build_candidate(
                            mode=minute.mode,
                            arm=arm,
                            side=side,
                            decision_ts=bar.ts,
                            reference_price=bar.close,
                            config=config,
                            cost_bps=cost_bps,
                            trigger={
                                "kind": "complete_1m_close_and_volume",
                                "body_ratio": _body_ratio(bar),
                                "relative_volume": relative_volume,
                            },
                        ),
                        arm.locked_ts,
                    )
                    minute.last_fire_ts = bar.ts

        current_bucket.append((index, bar))
        emitted = aggregator.on_one_minute(native, bar)
        for five in emitted:
            prior_arm = arm
            baseline = modes["close_5m"]
            if scoring and prior_arm is not None and _eligible(baseline, five.ts, config):
                side = _close_side(five, prior_arm, config)
                base_volume = prior_arm.five_minute_volume_median
                relative_volume = five.volume / base_volume if base_volume > 0 else 0.0
                previous_close = history[-1].close if history else five.open
                true_range = max(
                    five.high - five.low,
                    abs(five.high - previous_close),
                    abs(five.low - previous_close),
                )
                expansion = (
                    true_range / prior_arm.five_minute_atr
                    if prior_arm.five_minute_atr > 0
                    else 0.0
                )
                if (
                    side is not None
                    and _body_ratio(five) >= config.breakout.minimum_body_ratio
                    and relative_volume >= config.breakout.minimum_relative_volume
                    and expansion >= config.breakout.minimum_true_range_atr_multiple
                ):
                    candidate = _build_candidate(
                        mode=baseline.mode,
                        arm=prior_arm,
                        side=side,
                        decision_ts=five.ts,
                        reference_price=five.close,
                        config=config,
                        cost_bps=cost_bps,
                        trigger={
                            "kind": "complete_5m_close",
                            "body_ratio": _body_ratio(five),
                            "relative_volume": relative_volume,
                            "true_range_atr_multiple": expansion,
                        },
                    )
                    baseline.pending = (candidate, prior_arm.locked_ts)
                    baseline.last_fire_ts = five.ts

                    threshold = _threshold(prior_arm, side, config)
                    touch = next(
                        (
                            (source_index, source_bar)
                            for source_index, source_bar in current_bucket
                            if (
                                source_bar.high >= threshold
                                if side is Side.LONG
                                else source_bar.low <= threshold
                            )
                        ),
                        None,
                    )
                    if touch is not None:
                        source_index, source_bar = touch
                        fill = (
                            max(threshold, source_bar.open)
                            if side is Side.LONG
                            else min(threshold, source_bar.open)
                        )
                        oracle = _build_candidate(
                            mode="same_signal_boundary_oracle",
                            arm=prior_arm,
                            side=side,
                            decision_ts=source_bar.ts,
                            reference_price=fill,
                            config=config,
                            cost_bps=cost_bps,
                            trigger={
                                "kind": "future_5m_filter_selected_boundary_touch",
                                "non_deployable": True,
                                "confirming_5m_close": five.ts.isoformat(),
                            },
                        )
                        oracle_requests.append(
                            OracleRequest(
                                native,
                                oracle,
                                prior_arm.locked_ts,
                                source_index,
                                fill,
                            )
                        )

            history.append(five)
            change = detector.update(tuple(history))
            arm = _arm_from_history(native, history, arm_state, change, config)
            current_bucket.clear()
        one_minute_history.append(bar)

    dropped += sum(
        int(state.pending is not None) + int(state.position is not None)
        for state in modes.values()
    )
    oracle = _simulate_oracles(rows, oracle_requests, resolver)
    mode_results = {
        name: tuple(state.trades) for name, state in modes.items()
    }
    mode_results["same_signal_boundary_oracle"] = oracle
    return SymbolStudy(
        native,
        len(rows),
        missing,
        dropped,
        mode_results,
    )


def _profit_factor(trades: list[BacktestTrade]) -> float:
    gains = sum(row.net_bps for row in trades if row.net_bps > 0)
    losses = -sum(row.net_bps for row in trades if row.net_bps < 0)
    return gains / losses if losses else (float("inf") if gains else 0.0)


def _metrics(
    studies: tuple[SymbolStudy, ...], mode: str
) -> dict[str, object]:
    pairs = [pair for study in studies for pair in study.modes[mode]]
    trades = [pair[0] for pair in pairs]
    positions = [pair[1] for pair in pairs]
    net = [row.net_bps for row in trades]
    gross = [row.gross_bps for row in trades]
    mfe = [row.mfe_bps for row in trades]
    positive_capture = [
        min(1.0, row.gross_bps / row.mfe_bps)
        for row in trades
        if row.gross_bps > 0 and row.mfe_bps > 0
    ]
    market: dict[str, dict[str, object]] = {}
    for study in studies:
        rows = [pair[0] for pair in study.modes[mode]]
        market[study.symbol] = {
            "trades": len(rows),
            "average_net_bps": fmean(row.net_bps for row in rows) if rows else 0.0,
            "profit_factor": _profit_factor(rows),
        }
    return {
        "trades": len(trades),
        "average_gross_bps": fmean(gross) if gross else 0.0,
        "average_cost_bps": fmean(row.cost_bps for row in trades) if trades else 0.0,
        "average_net_bps": fmean(net) if net else 0.0,
        "net_bps": sum(net),
        "profit_factor": _profit_factor(trades),
        "win_rate": sum(value > 0 for value in net) / len(net) if net else 0.0,
        "average_mfe_bps": fmean(mfe) if mfe else 0.0,
        "average_mae_bps": fmean(row.mae_bps for row in trades) if trades else 0.0,
        "average_mfe_after_cost_bps": (
            fmean(row.mfe_bps - row.cost_bps for row in trades) if trades else 0.0
        ),
        "fee_wall_break_rate": (
            sum(row.mfe_bps > row.cost_bps for row in trades) / len(trades)
            if trades
            else 0.0
        ),
        "positive_trade_capture_ratio": (
            fmean(positive_capture) if positive_capture else 0.0
        ),
        "average_giveback_from_mfe_bps": (
            fmean(row.mfe_bps - row.gross_bps for row in trades)
            if trades
            else 0.0
        ),
        "average_time_to_mfe_seconds": (
            fmean(position.time_to_mfe_seconds for position in positions)
            if positions
            else 0.0
        ),
        "average_entry_delay_from_lock_seconds": (
            fmean(
                max(
                    0,
                    (trade.entry_ts - position.locked_ts).total_seconds(),
                )
                for trade, position in pairs
            )
            if pairs
            else 0.0
        ),
        "exit_reasons": dict(Counter(row.exit_reason for row in trades)),
        "same_bar_ambiguity_rate": (
            sum(row.same_bar_ambiguous for row in trades) / len(trades)
            if trades
            else 0.0
        ),
        "markets": market,
    }


async def run(args: argparse.Namespace) -> dict[str, object]:
    config_path = Path(args.config)
    config = load_range_compression_config(config_path)
    prior = json.loads(Path(args.v1_result).read_text(encoding="utf-8"))
    split = prior["split"]
    selection_end = _parse_ts(split["selection_decision_end"])
    process_end = _parse_ts(split["boundary"])
    start = _parse_ts(config.data.start)
    # The loader is explicitly capped at the split boundary.  The untouched
    # tail is not even read into this process.
    loaded = await asyncio.gather(
        *(
            _load_candles(
                symbol,
                start,
                process_end,
                cache_dir=Path(args.cache_dir),
                refresh=False,
            )
            for symbol in config.data.symbols
        )
    )
    studies = tuple(
        simulate_symbol(
            symbol,
            rows,
            config,
            selection_end=selection_end,
            process_end=process_end,
        )
        for symbol, rows in zip(config.data.symbols, loaded, strict=True)
    )
    metrics = {
        mode: _metrics(studies, mode)
        for mode in (
            "close_5m",
            "close_1m_volume",
            "boundary_stop",
            "same_signal_boundary_oracle",
        )
    }
    payload: dict[str, object] = {
        "report_id": "range_compression_early_entry_selection_study_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "research_status": "exploratory_selection_only",
        "selection_window": {
            "start": start.isoformat(),
            "decision_end": selection_end.isoformat(),
            "process_end": process_end.isoformat(),
        },
        "untouched": {
            "status": "sealed",
            "loaded": False,
            "predictions_computed": False,
            "trades_computed": False,
        },
        "controls": {
            "cost_bps": prior["selection"]["metrics"]["average_cost_bps"],
            "maximum_hold_seconds": config.exit.time_stop_seconds,
            "same_bar_resolution": "stop_first",
            "cooldown_minutes": config.breakout.cooldown_minutes,
            "one_active_observation_per_market": True,
            "range_and_compression": "frozen range_compression_breakout_v1",
            "tight_stop": "max(15 bps, 2 bps + 0.20 * ATR at range lock), capped at 60 bps",
            "target": "max(2.5R, 3.5 * modeled round-trip cost)",
        },
        "variant_validity": {
            "close_5m": "causal; complete 5m close/body/volume/expansion",
            "close_1m_volume": "causal; complete 1m close/body/volume; next 1m open",
            "boundary_stop": (
                "causal OHLC proxy for a resting taker stop; no future volume filter; "
                "same-minute path ambiguity is stop-first"
            ),
            "same_signal_boundary_oracle": (
                "non-deployable diagnostic; later 5m close selects earlier boundary fills"
            ),
        },
        "frozen_v1_reference": prior["selection"]["metrics"],
        "variants": metrics,
        "data_quality": {
            study.symbol: {
                "source_bars": study.source_bars,
                "missing_minutes": study.missing_minutes,
                "unresolved_dropped": study.unresolved_dropped,
            }
            for study in studies
        },
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "parameters_tunable_on_this_window": False,
        },
        "can_trade": False,
        "can_promote": False,
    }
    _atomic_json(Path(args.output), payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--v1-result", default=str(DEFAULT_V1_RESULT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser


def main() -> None:
    payload = asyncio.run(run(_parser().parse_args()))
    print(json.dumps(payload["variants"], indent=2))


if __name__ == "__main__":
    main()
