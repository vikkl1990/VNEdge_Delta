"""Safe local TradingView/Pine RuleSpec adapter tests."""

import json

import numpy as np
import pandas as pd
import pytest

from vnedge.research.tv_rule_adapter import (
    build_rule_artifact,
    compile_pine_rule_spec,
    evaluate_rule_spec,
    main,
)


def _candles(rows: int = 80) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC")
    close = pd.Series(np.linspace(100.0, 120.0, rows), index=index)
    close.iloc[40] = 130.0
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.where(np.arange(rows) == 40, 1000.0, 100.0),
        },
        index=index,
    )


def test_compiles_and_evaluates_causal_breakout_on_local_candles():
    source = """
//@version=6
strategy("Local Breakout", overlay=true)
length = input.int(20, "Lookback")
ema20 = ta.ema(close, length)
priorHigh = ta.highest(high[1], length)
volumeMean = ta.sma(volume, length)
long_signal = close > priorHigh and close > ema20 and volume > volumeMean * 2
if long_signal
    strategy.entry("L", strategy.long)
"""
    spec = compile_pine_rule_spec(
        source,
        title="Local Breakout",
        timeframe="5m",
        source_license="user-supplied",
    )

    assert spec.safe_for_local_replay is True
    assert spec.long_expression == "long_signal"
    assert spec.short_expression is None
    assert spec.can_trade is False
    assert spec.can_promote is False
    assert set(spec.supported_functions) == {"input.int", "ta.ema", "ta.highest", "ta.sma"}

    signals, evaluation = evaluate_rule_spec(spec, _candles())

    assert bool(signals.iloc[40]["long_signal"]) is True
    assert evaluation.long_signals >= 1
    assert evaluation.short_signals == 0
    assert len(evaluation.deterministic_hash) == 64
    _, repeated = evaluate_rule_spec(spec, _candles())
    assert repeated.deterministic_hash == evaluation.deterministic_hash


def test_alertcondition_can_define_indicator_signal_contract():
    source = """
//@version=6
indicator("EMA cross")
fast = ta.ema(close, 5)
slow = ta.ema(close, 10)
up = ta.crossover(fast, slow)
down = ta.crossunder(fast, slow)
alertcondition(up, "Long alert", "Buy")
alertcondition(down, "Short alert", "Sell")
"""
    spec = compile_pine_rule_spec(
        source,
        title="EMA Cross",
        timeframe="5m",
        source_license="MPL-2.0",
        provenance="public_open_source",
    )

    assert spec.safe_for_local_replay is True
    assert spec.long_expression == "up"
    assert spec.short_expression == "down"


@pytest.mark.parametrize(
    ("source", "expected_blocker"),
    [
        (
            'x = request.security(syminfo.tickerid, "60", close, lookahead=barmerge.lookahead_on)',
            "lookahead_on",
        ),
        ("future = close[-1]\nlong_signal = future > close", "future_bar_reference"),
        ("m = ta.macd(close, 12, 26, 9)\nlong_signal = m > 0", "unsupported_function:ta.macd"),
    ],
)
def test_repaint_and_unsupported_constructs_fail_closed(source, expected_blocker):
    spec = compile_pine_rule_spec(
        source,
        title="Unsafe",
        timeframe="5m",
        source_license="user-supplied",
    )

    assert spec.safe_for_local_replay is False
    assert expected_blocker in spec.blockers
    with pytest.raises(ValueError, match="rule is blocked"):
        evaluate_rule_spec(spec, _candles())


def test_unapproved_remote_provenance_and_license_are_quarantined():
    spec = compile_pine_rule_spec(
        "long_signal = close > open",
        title="Unknown Remote Script",
        timeframe="1h",
        source_license="unknown",
        provenance="catalog_metadata",
    )

    assert "source_license_not_approved" in spec.blockers
    assert "source_provenance_not_approved" in spec.blockers
    assert spec.safe_for_local_replay is False


def test_artifact_is_hash_only_research_contract_and_contains_no_source():
    source = "long_signal = close > ta.sma(close, 20)"
    spec = compile_pine_rule_spec(
        source,
        title="SMA Rule",
        timeframe="5m",
        source_license="user-supplied",
    )
    artifact = build_rule_artifact(spec)
    encoded = json.dumps(artifact)

    assert artifact["policy"] == {
        "network_access": False,
        "tradingview_data_used": False,
        "unofficial_tvscreener_dependency": False,
        "local_closed_candles_only": True,
        "raw_source_emitted": False,
        "normalized_rule_expressions_emitted": True,
        "unsupported_constructs_fail_closed": True,
        "research_only": True,
    }
    assert artifact["can_trade"] is False
    assert artifact["can_promote"] is False
    assert source not in encoded
    assert spec.source_sha256 in encoded


def test_cli_publishes_rule_contract_and_optional_signal_frame(tmp_path):
    source_path = tmp_path / "rule.pine"
    source_path.write_text(
        """
//@version=6
strategy("EMA")
average = ta.ema(close, 10)
long_signal = close > average
if long_signal
    strategy.entry("L", strategy.long)
""",
        encoding="utf-8",
    )
    candles_path = tmp_path / "candles.csv"
    _candles().rename_axis("timestamp").to_csv(candles_path)
    artifact_path = tmp_path / "artifact.json"
    signals_path = tmp_path / "signals.csv"

    rc = main(
        [
            "--source",
            str(source_path),
            "--title",
            "EMA",
            "--timeframe",
            "5m",
            "--candles",
            str(candles_path),
            "--signals-out",
            str(signals_path),
            "--out",
            str(artifact_path),
        ]
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert rc == 0
    assert payload["status"] == "READY_FOR_LOCAL_REPLAY"
    assert payload["evaluation"]["rows"] == 80
    assert signals_path.is_file()
    assert "strategy.entry" not in artifact_path.read_text(encoding="utf-8")


def test_evaluator_rejects_non_chronological_or_incomplete_candles():
    spec = compile_pine_rule_spec(
        "long_signal = close > open",
        title="Simple",
        timeframe="5m",
        source_license="user-supplied",
    )
    frame = _candles()
    with pytest.raises(ValueError, match="chronological"):
        evaluate_rule_spec(spec, frame.iloc[::-1])
    with pytest.raises(ValueError, match="missing required columns"):
        evaluate_rule_spec(spec, frame.drop(columns=["volume"]))


def test_full_history_and_prefix_evaluation_have_identical_past_signals():
    spec = compile_pine_rule_spec(
        """
fast = ta.ema(close, 5)
slow = ta.ema(close, 13)
long_signal = ta.crossover(fast, slow)
short_signal = ta.crossunder(fast, slow)
""",
        title="Causal EMA Cross",
        timeframe="5m",
        source_license="user-supplied",
    )
    frame = _candles(100)
    full, _ = evaluate_rule_spec(spec, frame)
    prefix, _ = evaluate_rule_spec(spec, frame.iloc[:65])

    pd.testing.assert_frame_equal(
        full.loc[prefix.index, ["long_signal", "short_signal"]],
        prefix[["long_signal", "short_signal"]],
    )
