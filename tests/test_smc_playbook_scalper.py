"""SMC playbook scalper research and gates."""

from __future__ import annotations

import pandas as pd

from vnedge.research.smc_playbook_scalper import (
    SMCPlaybookCandidate,
    parse_args,
    run_smc_playbook_research,
)
from vnedge.strategy.smc_playbook_scalper import (
    SMCPlaybookScalper,
    add_smc_playbook_columns,
)


def _playbook_row(**overrides) -> pd.DataFrame:
    row = {
        "timestamp": pd.Timestamp("2026-07-14T00:00:00Z"),
        "open": 99.0,
        "high": 101.0,
        "low": 98.5,
        "close": 100.0,
        "volume": 10_000.0,
        "atr": 2.0,
        "atr_pct": 0.55,
        "external_liquidity_high": 110.0,
        "external_liquidity_low": 90.0,
        "smc_bull_zone_floor": 98.0,
        "smc_bull_zone_ceiling": 99.2,
        "smc_bear_zone_floor": 100.8,
        "smc_bear_zone_ceiling": 102.0,
        "smc_long_quality": 5.0,
        "smc_short_quality": 1.0,
        "smc_long_setup": True,
        "smc_short_setup": False,
        "smc_trigger_long": True,
        "smc_trigger_short": False,
        "smc_sweep_low_recent": True,
        "smc_bull_zone_recent": True,
        "smc_choch_up_recent": True,
        "smc_in_discount": True,
        "smc_in_premium": False,
        "sweep_low": True,
        "sweep_high": False,
        "volatility_ok": True,
        "funding_rate": 0.0,
        "ctx_1h_bias_long": True,
        "ctx_1h_bos_up": False,
        "ctx_1h_choch_up": False,
        "ctx_1h_bias_short": False,
        "ctx_1h_bos_down": False,
        "ctx_1h_choch_down": False,
        "ctx_4h_bias_long": False,
        "ctx_4h_bos_up": False,
        "ctx_4h_choch_up": False,
        "ctx_4h_bias_short": False,
        "ctx_4h_bos_down": False,
        "ctx_4h_choch_down": False,
        "trigger_1m_momentum_long": True,
        "trigger_1m_momentum_short": False,
        "trigger_1m_long": True,
        "trigger_1m_short": False,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_smc_playbook_requires_full_sequence_and_plans_exit():
    strategy = SMCPlaybookScalper()

    intent = strategy.signal(_playbook_row(), 0)

    assert intent is not None
    assert intent.side == "long"
    assert intent.stop_price < 100.0
    assert intent.take_profit_price is not None
    assert intent.take_profit_price > 100.0
    assert "setup=sweep+zone+choch" in intent.reason
    assert "tp1=" in intent.reason
    assert "be_after_tp1=true" in intent.reason


def test_smc_playbook_blocks_without_room_to_liquidity():
    strategy = SMCPlaybookScalper(min_room_r=1.2)

    intent = strategy.signal(_playbook_row(external_liquidity_high=100.5), 0)

    assert intent is None


def test_smc_playbook_blocks_missing_htf_context():
    strategy = SMCPlaybookScalper()

    intent = strategy.signal(_playbook_row(ctx_1h_bias_long=False), 0)

    assert intent is None


def test_smc_playbook_features_are_causal_under_future_mutation():
    timestamps = pd.date_range("2026-07-01", periods=260, freq="15min", tz="UTC")
    base = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0 + i * 0.03 for i in range(260)],
            "high": [100.5 + i * 0.03 for i in range(260)],
            "low": [99.5 + i * 0.03 for i in range(260)],
            "close": [100.1 + i * 0.03 for i in range(260)],
            "volume": [1000.0 + (i % 7) * 10.0 for i in range(260)],
        }
    )
    mutated = base.copy()
    mutated.loc[180:, "close"] += 25.0
    mutated.loc[180:, "high"] += 25.0
    mutated.loc[180:, "low"] += 25.0

    original_features = add_smc_playbook_columns(base)
    mutated_features = add_smc_playbook_columns(mutated)

    pd.testing.assert_frame_equal(
        original_features.iloc[:160].reset_index(drop=True),
        mutated_features.iloc[:160].reset_index(drop=True),
        check_dtype=False,
    )


def test_smc_playbook_research_marks_missing_lanes_untestable(tmp_path):
    candidate = SMCPlaybookCandidate("binanceusdm", "ETH/USDT:USDT")

    report = run_smc_playbook_research(tmp_path, candidates=(candidate,))

    result = report["results"][0]
    assert report["strategy"] == "smc_playbook_scalper_v1"
    assert report["policy"]["can_trade"] is False
    assert result["verdict"] == "UNTESTABLE"
    assert "missing data lane" in result["reasons"][0]


def test_smc_playbook_cli_supports_smoke_and_candidates():
    args = parse_args(
        [
            "--once",
            "--fast-smoke",
            "--candidate",
            "delta_india|ETH/USD:USD|long",
        ]
    )

    assert args.once is True
    assert args.fast_smoke is True
    assert args.candidate[0].exchange == "delta_india"
    assert args.candidate[0].allowed_sides == ("long",)
