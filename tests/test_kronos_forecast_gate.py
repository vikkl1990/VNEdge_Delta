import pandas as pd

from vnedge.research.kronos_forecast_gate import (
    KronosForecastGateConfig,
    build_kronos_forecast_gate_report,
    score_kronos_forecast_gate,
)


def _context(close: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame([
        {"open": 99.0, "high": 101.0, "low": 98.0, "close": close},
    ])


def _forecast(values, *, sample_id: str | None = None) -> pd.DataFrame:
    rows = []
    for row in values:
        item = {
            "open": row[0],
            "high": row[1],
            "low": row[2],
            "close": row[3],
        }
        if sample_id is not None:
            item["sample_id"] = sample_id
        rows.append(item)
    return pd.DataFrame(rows)


def test_kronos_forecast_gate_passes_large_long_path_after_fee_wall():
    decision = score_kronos_forecast_gate(
        _context(),
        _forecast([
            (100.2, 101.2, 99.8, 100.8),
            (100.8, 104.0, 100.4, 103.5),
        ]),
        config=KronosForecastGateConfig(
            min_expected_net_bps=25.0,
            min_confidence=0.50,
            max_adverse_bps=80.0,
        ),
    )

    assert decision.verdict == "FORECAST_GATE_PASS"
    assert decision.selected_side == "long"
    assert decision.recommended_action == "ALLOW_MAKER_RESEARCH_ONLY"
    assert decision.scores["long"]["expected_net_bps"] > 25.0
    assert decision.can_trade is False
    assert decision.can_promote is False


def test_kronos_forecast_gate_selects_short_when_downside_path_is_better():
    decision = score_kronos_forecast_gate(
        _context(),
        _forecast([
            (99.5, 100.2, 98.4, 98.8),
            (98.8, 99.0, 96.0, 96.5),
        ]),
        config=KronosForecastGateConfig(min_confidence=0.50),
    )

    assert decision.verdict == "FORECAST_GATE_PASS"
    assert decision.selected_side == "short"
    assert decision.scores["short"]["expected_net_bps"] > decision.scores["long"]["expected_net_bps"]


def test_kronos_forecast_gate_blocks_small_move_after_costs():
    decision = score_kronos_forecast_gate(
        _context(),
        _forecast([
            (100.0, 100.2, 99.9, 100.1),
            (100.1, 100.3, 99.9, 100.2),
        ]),
    )

    assert decision.verdict == "FORECAST_TOO_SMALL_AFTER_COSTS"
    assert "expected net" in decision.primary_blocker
    assert decision.recommended_action == "SKIP"


def test_kronos_forecast_gate_blocks_low_sample_agreement():
    mixed = pd.concat([
        _forecast([(100.1, 104.0, 99.8, 103.0)], sample_id="up"),
        _forecast([(99.9, 100.1, 97.0, 97.2)], sample_id="down"),
    ], ignore_index=True)

    decision = score_kronos_forecast_gate(
        _context(),
        mixed,
        config=KronosForecastGateConfig(
            min_expected_net_bps=10.0,
            min_confidence=0.75,
            min_reward_risk=0.0,
            max_adverse_bps=400.0,
        ),
        side="long",
    )

    assert decision.verdict == "FORECAST_CONFIDENCE_TOO_LOW"
    assert decision.scores["long"]["samples"] == 2
    assert decision.scores["long"]["confidence"] == 0.5


def test_kronos_forecast_gate_report_stays_read_only():
    report = build_kronos_forecast_gate_report([
        {
            "lane_id": "alpha",
            "exchange": "binanceusdm",
            "symbol": "ETH/USDT:USDT",
            "timeframe": "15m",
            "context": _context().to_dict("records"),
            "forecast": _forecast([
                (100.2, 101.2, 99.8, 100.8),
                (100.8, 104.0, 100.4, 103.5),
            ]).to_dict("records"),
        }
    ])

    assert report["report_id"] == "kronos_forecast_gate_v1"
    assert report["summary"]["rows"] == 1
    assert report["summary"]["passes"] == 1
    assert report["rows"][0]["lane_id"] == "alpha"
    assert report["can_trade"] is False
    assert report["can_promote"] is False
