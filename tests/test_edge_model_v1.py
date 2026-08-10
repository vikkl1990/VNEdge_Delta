"""edge_model_v1 learns from opportunity rows without route authority."""

from datetime import UTC

import pandas as pd

from vnedge.research.edge_model_v1 import (
    EdgeModelConfig,
    _feature_columns,
    _target_data_coverage,
    backtest_edge_model,
    backtest_edge_model_timeframe_matrix,
    build_opportunity_dataset,
    load_strategy_opportunities,
)
from vnedge.research.execution_edge_router import OpportunityRoute
from vnedge.research.universe import ResearchTarget


def route(
    i: int,
    *,
    edge_score: float,
    maker_net: float,
    taker_net: float | None = None,
    timeframe: str = "15m",
    prefix: str = "event",
    expected_edge_bps: float | None = None,
):
    ts = pd.Timestamp("2026-07-01T00:00:00Z") + pd.Timedelta(minutes=15 * i)
    return OpportunityRoute(
        event_id=f"{prefix}-{i}",
        ts=ts.isoformat(),
        side="long" if i % 2 == 0 else "short",
        source_id="test",
        strategy_id="test_scanner_v1",
        action="MAKER",
        reason="maker baseline routed without edge forecast",
        selected_route="MAKER_ONLY",
        selected_net_bps=maker_net,
        selected_gross_bps=maker_net + 7.0,
        selected_cost_bps=7.0,
        maker_net_bps=maker_net,
        maker_gross_bps=maker_net + 7.0,
        maker_cost_bps=7.0,
        taker_net_bps=maker_net - 5.0 if taker_net is None else taker_net,
        taker_gross_bps=maker_net + 7.0,
        taker_cost_bps=12.0,
        maker_fill_probability=0.60,
        expected_edge_bps=expected_edge_bps,
        outcome="target" if maker_net > 0 else "stop",
        mfe_bps=abs(maker_net) + 12.0,
        mae_bps=-12.0,
        risk_bps=20.0,
        metadata={
            "exchange": "test",
            "symbol": "ETH/USDT:USDT",
            "timeframe": timeframe,
            "edge_score": edge_score,
            "reason": f"demo score={edge_score:.1f}; features=impulse",
        },
    )


def make_routes(n: int = 220, *, timeframe: str = "15m", prefix: str = "event"):
    rows = []
    for i in range(n):
        good = i % 4 in {0, 1}
        rows.append(
            route(
                i,
                edge_score=1.0 if good else 0.0,
                maker_net=45.0 if good else -15.0,
                timeframe=timeframe,
                prefix=prefix,
            )
        )
    return tuple(rows)


def test_edge_model_improves_oos_by_selecting_stable_feature():
    report = backtest_edge_model(
        make_routes(),
        config=EdgeModelConfig(
            train_fraction=0.70,
            min_train_samples=100,
            min_test_samples=40,
            min_model_trades=20,
            min_predicted_net_bps=25.0,
            min_profit_factor=1.5,
        ),
    )
    summary = report["summary"]

    assert summary["verdict"] == "MODEL_PAPER_CANDIDATE"
    assert summary["can_trade"] is False
    assert summary["can_promote"] is False
    assert summary["model_avg_net_bps"] > summary["raw_avg_net_bps"]
    assert summary["improvement_bps"] >= 20.0
    assert summary["model_profit_factor"] == 999.0
    assert summary["model_trades"] >= 20
    assert report["policy"]["decision_uses_forward_truth"] is False


def test_feature_builder_excludes_forward_truth_columns():
    df = build_opportunity_dataset(
        (
            *make_routes(20),
            route(
                21,
                edge_score=0.8,
                maker_net=30.0,
                timeframe="5m",
                prefix="feature",
                expected_edge_bps=40.0,
            ),
        )
    )
    features = set(_feature_columns(df))

    assert "maker_net_bps" not in features
    assert "taker_net_bps" not in features
    assert "event_id" not in features
    assert "ts" not in features
    assert "meta_edge_score" in features
    assert "reason_score" in features
    assert "reason_feature_impulse" in features
    assert "timeframe_seconds" in features
    assert "expected_minus_maker_cost_bps" in features
    assert "maker_fill_edge_bps" in features
    assert df["ts"].iloc[0].tzinfo == UTC


def test_edge_model_under_sampled_before_training():
    report = backtest_edge_model(
        make_routes(20),
        config=EdgeModelConfig(min_train_samples=100, min_test_samples=10),
    )

    assert report["summary"]["verdict"] == "UNDER_SAMPLED"
    assert report["summary"]["model_trades"] == 0
    assert "need >=" in report["summary"]["primary_blocker"]


def test_timeframe_matrix_reports_aggregate_and_each_slice():
    routes = (
        *make_routes(180, timeframe="15m", prefix="fifteen"),
        *make_routes(180, timeframe="1h", prefix="hour"),
    )
    report = backtest_edge_model_timeframe_matrix(
        routes,
        config=EdgeModelConfig(
            train_fraction=0.70,
            min_train_samples=80,
            min_test_samples=30,
            min_model_trades=20,
        ),
    )

    assert report["truth_layer"] == "edge_model_v1_timeframe_matrix"
    assert report["policy"]["can_trade"] is False
    assert report["summary"]["timeframes"] == ["15m", "1h"]
    assert report["summary"]["timeframe_count"] == 2
    assert report["summary"]["total_opportunities"] == 360

    scopes = {item["scope"]["id"]: item for item in report["reports"]}
    assert set(scopes) == {"ALL_TIMEFRAMES", "15m", "1h"}
    assert scopes["ALL_TIMEFRAMES"]["policy"]["decision_uses_forward_truth"] is False
    assert scopes["15m"]["summary"]["opportunities"] == 180
    assert scopes["1h"]["summary"]["opportunities"] == 180


def test_target_data_coverage_reports_missing_timeframe_without_reading_parquet(tmp_path):
    available = ResearchTarget("delta_india", "ETH/USD:USD", "5m")
    missing = ResearchTarget("delta_india", "ETH/USD:USD", "1m")
    path = (
        tmp_path
        / "normalized"
        / "exchange=delta_india"
        / "symbol=ETHUSD"
        / "timeframe=5m"
        / "candles.parquet"
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a parquet file; coverage only checks existence")

    coverage = _target_data_coverage(tmp_path, (available, missing))

    assert coverage["attempted"] == 2
    assert coverage["available"] == 1
    assert coverage["missing"] == 1
    assert coverage["available_targets"] == [
        {"exchange": "delta_india", "symbol": "ETH/USD:USD", "timeframe": "5m"}
    ]
    assert coverage["missing_targets"] == [
        {"exchange": "delta_india", "symbol": "ETH/USD:USD", "timeframe": "1m"}
    ]


def test_load_strategy_opportunities_reports_progress_for_missing_candles(tmp_path):
    events = []
    routes = load_strategy_opportunities(
        data_root=tmp_path,
        targets=(ResearchTarget("delta_india", "ETH/USD:USD", "5m"),),
        strategy_ids=("stealth_trail_bbp_v1", "luxara_live_plan_qtm_v1"),
        progress_callback=events.append,
    )

    assert routes == ()
    assert events[0]["phase"] == "missing_candles"
    assert events[0]["completed_work_units"] == 0
    assert events[-1]["completed_work_units"] == 2
    assert events[-1]["total_work_units"] == 2
    assert events[-1]["target"] == {
        "exchange": "delta_india",
        "symbol": "ETH/USD:USD",
        "timeframe": "5m",
    }
