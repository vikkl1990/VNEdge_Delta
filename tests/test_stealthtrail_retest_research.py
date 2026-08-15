"""Causality and accounting tests for the StealthTrail-derived research contracts."""

import pandas as pd
import pytest

from vnedge.research.stealthtrail_retest_research import (
    DEFAULT_RETEST_CONFIG,
    _funding_cashflow_bps,
    _resolve_scalper_bar,
)


def _active() -> dict[str, object]:
    return {
        "side": "long",
        "decision_ts": pd.Timestamp("2026-01-01T00:00:00Z"),
        "entry_ts": pd.Timestamp("2026-01-01T00:01:00Z"),
        "entry": 100.0,
        "stop": 99.0,
        "tp1_price": 101.0,
        "tp2_price": 102.5,
        "risk_bps": 100.0,
        "target_bps": 250.0,
        "tp1_done": False,
        "mfe": 0.0,
        "mae": 0.0,
    }


def test_scalper_entry_bar_ambiguity_is_resolved_stop_first():
    active = _active()
    bar = pd.Series({"open": 100.0, "high": 103.0, "low": 98.0, "close": 102.0})

    result = _resolve_scalper_bar(
        active,
        bar,
        pd.Timestamp("2026-01-01T00:02:00Z"),
        symbol="ETHUSD",
        config=DEFAULT_RETEST_CONFIG,
    )

    assert result is not None
    assert result.exit_reason == "stop"
    assert result.partial_tp1 is False
    assert result.gross_bps == pytest.approx(-100.0)
    assert result.net_bps == pytest.approx(-114.8)


def test_partial_exit_realizes_half_at_one_r_then_half_at_final_price():
    active = _active()
    first = pd.Series({"open": 100.0, "high": 101.2, "low": 99.5, "close": 101.0})
    second = pd.Series({"open": 101.0, "high": 102.6, "low": 100.5, "close": 102.5})

    assert (
        _resolve_scalper_bar(
            active,
            first,
            pd.Timestamp("2026-01-01T00:02:00Z"),
            symbol="BTCUSD",
            config=DEFAULT_RETEST_CONFIG,
        )
        is None
    )
    result = _resolve_scalper_bar(
        active,
        second,
        pd.Timestamp("2026-01-01T00:03:00Z"),
        symbol="BTCUSD",
        config=DEFAULT_RETEST_CONFIG,
    )

    assert result is not None
    assert result.exit_reason == "tp2"
    assert result.partial_tp1 is True
    assert result.gross_bps == pytest.approx(175.0)
    assert result.net_bps == pytest.approx(160.2)


def test_funding_uses_only_rates_available_at_each_settlement_checkpoint():
    funding = pd.DataFrame(
        {
            "available_at": pd.to_datetime(
                ["2026-01-01T07:00:00Z", "2026-01-01T09:00:00Z", "2026-01-01T15:00:00Z"],
                utc=True,
            ),
            "funding_rate": [0.0001, 0.0099, -0.0002],
        }
    )

    long_cost = _funding_cashflow_bps(
        funding,
        side="long",
        entry_ts=pd.Timestamp("2026-01-01T06:00:00Z"),
        exit_ts=pd.Timestamp("2026-01-01T17:00:00Z"),
        interval_hours=8,
    )
    short_cost = _funding_cashflow_bps(
        funding,
        side="short",
        entry_ts=pd.Timestamp("2026-01-01T06:00:00Z"),
        exit_ts=pd.Timestamp("2026-01-01T17:00:00Z"),
        interval_hours=8,
    )

    # 08:00 sees +1 bps; 16:00 sees -2 bps. The 09:00 value is replaced
    # before the second settlement and is never applied retroactively.
    assert long_cost == pytest.approx(-1.0)
    assert short_cost == pytest.approx(1.0)
