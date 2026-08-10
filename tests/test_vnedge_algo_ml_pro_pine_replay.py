"""Pine-parity replay tests for VNEDGE Algo ML Pro."""

import pytest
import pandas as pd

from vnedge.exchange.delta_contracts import DeltaContractSpec
from vnedge.research.vnedge_algo_ml_pro_pine_replay import (
    PineReplayConfig,
    replay_prepared_vnedge_algo_ml_pro,
    summarize_pine_replay_trades,
)
from vnedge.strategy.vnedge_algo_ml_pro import VNEDGEAlgoMLProParams


def _prepared(rows: list[dict]) -> pd.DataFrame:
    base = pd.Timestamp("2026-07-21T00:00:00Z")
    out = pd.DataFrame(rows)
    out["timestamp"] = [base + pd.Timedelta(minutes=5 * i) for i in range(len(out))]
    out["volume"] = 100.0
    out["atr_base"] = out["atr_value"]
    if "confirmed_long" not in out:
        out["confirmed_long"] = False
    if "confirmed_short" not in out:
        out["confirmed_short"] = False
    out["confirmed_long"] = out["confirmed_long"].fillna(False)
    out["confirmed_short"] = out["confirmed_short"].fillna(False)
    return out


def test_pine_replay_enters_on_signal_close_and_stop_is_close_based():
    df = _prepared(
        [
            {
                "open": 99.0,
                "high": 100.0,
                "low": 98.5,
                "close": 100.0,
                "atr_value": 1.0,
                "st_band": 98.0,
                "confirmed_long": True,
            },
            {
                "open": 100.0,
                "high": 102.1,
                "low": 97.0,
                "close": 100.5,
                "atr_value": 1.0,
                "st_band": 99.0,
            },
            {
                "open": 100.5,
                "high": 106.1,
                "low": 100.0,
                "close": 104.0,
                "atr_value": 1.0,
                "st_band": 101.0,
            },
        ]
    )
    config = PineReplayConfig(fee_cost_bps=12.0, mark_open_at_end=False)

    trades = replay_prepared_vnedge_algo_ml_pro(
        df,
        params=VNEDGEAlgoMLProParams(use_mtf=False),
        config=config,
        fee_cost_bps=12.0,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.entry_index == 0
    assert trade.entry_price == 100.0
    assert trade.exit_reason == "TP3"
    assert trade.exit_price == 106.0
    assert trade.tp1_hit is True
    assert trade.tp3_hit is True
    assert trade.gross_bps == pytest.approx(600.0)
    assert trade.fee_aware_net_bps == pytest.approx(588.0)
    assert trade.paper_fee_aware_usd == pytest.approx(147.0)


def test_pine_replay_reverses_at_current_signal_close():
    df = _prepared(
        [
            {
                "open": 99.5,
                "high": 100.2,
                "low": 98.8,
                "close": 100.0,
                "atr_value": 1.0,
                "st_band": 98.0,
                "confirmed_long": True,
            },
            {
                "open": 100.0,
                "high": 101.5,
                "low": 99.5,
                "close": 101.0,
                "atr_value": 1.0,
                "st_band": 103.0,
                "confirmed_short": True,
            },
        ]
    )

    trades = replay_prepared_vnedge_algo_ml_pro(
        df,
        params=VNEDGEAlgoMLProParams(use_mtf=False),
        config=PineReplayConfig(mark_open_at_end=False),
        fee_cost_bps=10.0,
    )

    assert len(trades) == 1
    assert trades[0].exit_reason == "REVERSE"
    assert trades[0].exit_price == 101.0
    assert trades[0].gross_bps == pytest.approx(100.0)


def test_pine_replay_summary_keeps_visual_and_fee_aware_results_separate():
    df = _prepared(
        [
            {
                "open": 99.0,
                "high": 100.0,
                "low": 98.0,
                "close": 100.0,
                "atr_value": 1.0,
                "st_band": 98.0,
                "confirmed_long": True,
            },
            {
                "open": 100.0,
                "high": 106.2,
                "low": 99.0,
                "close": 103.0,
                "atr_value": 1.0,
                "st_band": 101.0,
            },
        ]
    )
    config = PineReplayConfig(fee_cost_bps=12.0, mark_open_at_end=False)
    trades = replay_prepared_vnedge_algo_ml_pro(
        df,
        params=VNEDGEAlgoMLProParams(use_mtf=False),
        config=config,
        fee_cost_bps=12.0,
    )

    summary = summarize_pine_replay_trades(trades, config=config)

    assert summary["visual_avg_bps"] == pytest.approx(600.0)
    assert summary["fee_aware_avg_bps"] == pytest.approx(588.0)
    assert summary["visual_paper_usd"] == pytest.approx(150.0)
    assert summary["fee_aware_paper_usd"] == pytest.approx(147.0)
    assert summary["bar_timing"]["entry_bar"] == "signal_close"
    assert summary["bar_timing"]["fixed_exit_wait_bars"] is None
    assert summary["bar_timing"]["first_exit_check_delay_bars"] == 1
    assert summary["bar_timing"]["self_learning_eval_horizon_bars"] == 15
    assert summary["bar_timing"]["self_learning_horizon_affects_trade_exit"] is False
    assert summary["hold_bars"]["avg"] == pytest.approx(1.0)
    assert summary["hold_bars"]["median"] == 1
    assert summary["hold_bars"]["by_exit_reason_avg"] == {"TP3": 1.0}
    assert summary["promotion_gate"]["passed"] is False


def test_smart_ladder_captures_tp1_and_moves_runner_to_breakeven():
    df = _prepared(
        [
            {
                "open": 99.0,
                "high": 100.0,
                "low": 98.5,
                "close": 100.0,
                "atr_value": 1.0,
                "st_band": 98.0,
                "confirmed_long": True,
            },
            {
                "open": 100.0,
                "high": 102.2,
                "low": 99.4,
                "close": 101.0,
                "atr_value": 1.0,
                "st_band": 99.0,
            },
            {
                "open": 101.0,
                "high": 101.2,
                "low": 98.5,
                "close": 99.0,
                "atr_value": 1.0,
                "st_band": 99.0,
            },
        ]
    )
    config = PineReplayConfig(
        fee_cost_bps=12.0,
        capture_mode="smart_ladder",
        tp1_capture_fraction=0.35,
        tp2_capture_fraction=0.35,
        mark_open_at_end=False,
    )

    trades = replay_prepared_vnedge_algo_ml_pro(
        df,
        params=VNEDGEAlgoMLProParams(use_mtf=False),
        config=config,
        fee_cost_bps=12.0,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.capture_mode == "smart_ladder"
    assert trade.exit_reason == "SL"
    assert trade.tp1_hit is True
    assert trade.tp2_hit is False
    assert trade.remaining_fraction_closed_at_final == pytest.approx(0.65)
    assert trade.realized_gross_bps_before_final == pytest.approx(70.0)
    assert trade.gross_bps == pytest.approx(5.0)
    assert trade.fee_aware_net_bps == pytest.approx(-7.0)

    summary = summarize_pine_replay_trades(trades, config=config)
    assert summary["capture_mode"] == "smart_ladder"
    assert summary["bar_timing"]["tp1_tp2_are_markers_only"] is False
    assert summary["smart_capture"]["enabled"] is True
    assert summary["smart_capture"]["runner_fraction"] == pytest.approx(0.30)


def test_delta_contract_risk_sizing_uses_rounded_contract_notional():
    df = _prepared(
        [
            {
                "open": 1878.0,
                "high": 1882.0,
                "low": 1870.0,
                "close": 1881.0,
                "atr_value": 5.0,
                "st_band": 1856.0,
                "confirmed_long": True,
            },
            {
                "open": 1881.0,
                "high": 1912.0,
                "low": 1880.0,
                "close": 1911.0,
                "atr_value": 5.0,
                "st_band": 1890.0,
            },
        ]
    )
    config = PineReplayConfig(
        sizing_mode="delta_contract_risk",
        delta_contract_spec=DeltaContractSpec(
            symbol="ETHUSD",
            product_id=3136,
            contract_value=0.01,
            contract_unit_currency="ETH",
            initial_margin_pct=0.5,
            maintenance_margin_pct=0.25,
        ),
        account_equity_usd=500.0,
        risk_per_trade_pct=1.0,
        paper_leverage=25.0,
        acknowledge_high_leverage=True,
        fee_cost_bps=0.0,
        mark_open_at_end=False,
    )

    trades = replay_prepared_vnedge_algo_ml_pro(
        df,
        params=VNEDGEAlgoMLProParams(use_mtf=False, use_trailing=False),
        config=config,
        fee_cost_bps=0.0,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.paper_contracts == 20
    assert trade.paper_base_quantity == pytest.approx(0.2)
    assert trade.paper_notional_usd == pytest.approx(376.2)
    assert trade.paper_margin_usd == pytest.approx(15.048)
    assert trade.paper_leverage == pytest.approx(25.0)
    assert trade.paper_fee_aware_usd == pytest.approx(6.0)

    summary = summarize_pine_replay_trades(trades, config=config)
    assert summary["position_sizing"]["mode"] == "delta_contract_risk"
    assert summary["position_sizing"]["contracts_avg"] == pytest.approx(20.0)
