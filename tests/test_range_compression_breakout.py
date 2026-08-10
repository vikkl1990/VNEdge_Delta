from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.research.range_compression_backtest import (
    CompressionReplayResult,
    selection_gate,
    simulate_symbol_window,
)
from vnedge.scalping.delta_engine.range_compression import (
    RangeCompressionBreakoutScanner,
    RangeCompressionContext,
    load_range_compression_config,
    range_compression_fee_model,
)
from vnedge.scalping.delta_engine.types import Candle, ChangePointProfile, Side

CONFIG_PATH = "configs/research/range_compression_breakout_v1.yaml"
NOW = datetime(2026, 7, 25, tzinfo=UTC)


def _five_minute_setup() -> tuple[Candle, ...]:
    rows: list[Candle] = []
    close = 100.0
    total = 233
    for index in range(total - 1):
        ts = NOW - timedelta(minutes=5 * (total - index - 1))
        if index < total - 14:
            next_close = 100.0 + math.sin(index / 2) * 0.8
            span = 0.35
        else:
            next_close = 100.0 + math.sin(index) * 0.01
            span = 0.02
        rows.append(
            Candle(
                ts,
                close,
                max(close, next_close) + span,
                min(close, next_close) - span,
                next_close,
                100.0,
                "5m",
            )
        )
        close = next_close
    prior_high = max(row.high for row in rows[-12:])
    breakout_close = prior_high * 1.0030
    rows.append(
        Candle(
            NOW,
            prior_high * 0.9998,
            breakout_close * 1.0001,
            prior_high * 0.9997,
            breakout_close,
            200.0,
            "5m",
        )
    )
    return tuple(rows)


def _recent_shift() -> ChangePointProfile:
    return ChangePointProfile(
        source_timeframe="5m",
        detector_ready=True,
        regime_shift=True,
        volatility_shift=True,
        bars_since_shift=0,
        minutes_since_shift=0,
    )


def test_contract_is_frozen_research_only_and_costed_conservatively():
    config = load_range_compression_config(CONFIG_PATH)
    fee = range_compression_fee_model(config)

    assert config.contract_id == "range_compression_breakout_v1"
    assert config.can_trade is False and config.can_promote is False
    assert config.validation.open_untouched_only_after_selection_pass is True
    assert fee.breakdown(
        "BTCUSD", entry_is_maker=False, hold_seconds=900
    ).total_bps == pytest.approx(14.8)


def test_scanner_emits_one_causal_costed_breakout():
    config = load_range_compression_config(CONFIG_PATH)
    scanner = RangeCompressionBreakoutScanner(config, range_compression_fee_model(config))
    rows = _five_minute_setup()
    context = RangeCompressionContext("BTCUSD", NOW, rows, _recent_shift())

    candidate = scanner.evaluate(context)

    assert candidate is not None
    assert candidate.side is Side.LONG
    assert candidate.modeled_cost_bps == pytest.approx(14.8)
    assert candidate.metadata["compressed_bar_count"] >= 6
    assert candidate.metadata["cusum"]["regime_shift"] is True
    assert candidate.metadata["structural_prior"]["promotion_eligible"] is False
    assert scanner.evaluate(context) is None


def test_scanner_rejects_breakout_without_recent_cusum_shift():
    config = load_range_compression_config(CONFIG_PATH)
    scanner = RangeCompressionBreakoutScanner(config, range_compression_fee_model(config))
    rows = _five_minute_setup()
    stale = ChangePointProfile(source_timeframe="5m", detector_ready=True)

    assert scanner.evaluate(RangeCompressionContext("BTCUSD", NOW, rows, stale)) is None


def test_replay_gap_resets_and_fails_quality_gate():
    config = load_range_compression_config(CONFIG_PATH)
    rows = [
        Candle(
            NOW + timedelta(minutes=index + int(index >= 5)),
            100.0,
            100.1,
            99.9,
            100.0,
            10.0,
            "1m",
        )
        for index in range(10)
    ]

    result = simulate_symbol_window("BTCUSD", rows, config)

    assert result.missing_minutes == 1
    assert result.trades == ()


def test_selection_gate_fails_closed_without_standalone_evidence():
    config = load_range_compression_config(CONFIG_PATH)
    start = NOW - timedelta(days=2)
    empty = tuple(
        CompressionReplayResult(symbol, (), 0, 0, 100, 20, start, NOW)
        for symbol in config.data.symbols
    )

    gate = selection_gate(empty, config)

    assert gate["passed"] is False
    assert gate["checks"]["minimum_trades"] is False
    assert gate["checks"]["positive_markets"] is False
