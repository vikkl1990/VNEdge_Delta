from __future__ import annotations

import json

import pandas as pd

from vnedge.research.delta_scalper_triple_barrier_labels import (
    enrich_triple_barrier_labels,
    load_journal,
    run,
)


def test_loads_market_trade_shape_and_uses_explicit_path_label(tmp_path):
    source = tmp_path / "backtest.json"
    source.write_text(
        json.dumps(
            {
                "markets": {
                    "BTCUSD": {
                        "trades": [
                            {
                                "exit_reason": "target_1",
                                "net_bps": 8.0,
                                "mfe_bps": 12.0,
                                "planned_target_bps": 10.0,
                                "triple_barrier_label": 1,
                            },
                            {
                                "exit_reason": "stop",
                                "net_bps": -10.0,
                                "mfe_bps": 3.0,
                                "planned_target_bps": 10.0,
                                "triple_barrier_label": 0,
                            },
                        ]
                    }
                }
            }
        )
    )

    frame = load_journal(source)
    trades, summary = enrich_triple_barrier_labels(frame)

    assert trades["symbol"].tolist() == ["BTCUSD", "BTCUSD"]
    assert trades["tb_label"].tolist() == [1, 0]
    assert trades["tb_label_source"].tolist() == [
        "shared_path_simulator",
        "shared_path_simulator",
    ]
    assert summary["explicit_shared_simulator_labels"] == 2
    assert summary["tb_vs_net_gt_4bps_disagreements"] == 0


def test_time_stop_mfe_recovery_is_disabled_by_default_and_marked_when_enabled():
    frame = pd.DataFrame(
        [
            {
                "forward_outcome.exit_reason": "time_stop",
                "forward_outcome.net_bps": 6.0,
                "forward_outcome.mfe_bps": 15.0,
                "forward_outcome.planned_target_bps": 10.0,
            }
        ]
    )

    strict, strict_summary = enrich_triple_barrier_labels(frame)
    approximate, approximate_summary = enrich_triple_barrier_labels(
        frame,
        allow_mfe_time_stop_recovery=True,
    )

    assert strict.loc[0, "tb_label"] == 0
    assert strict.loc[0, "tb_first_barrier"] == "vertical"
    assert strict_summary["mfe_time_stop_labels_recovered"] == 0
    assert approximate.loc[0, "tb_label"] == 1
    assert approximate.loc[0, "tb_label_source"] == "mfe_time_stop_approximation"
    assert approximate_summary["mfe_time_stop_labels_recovered"] == 1


def test_run_writes_parquet_and_summary(tmp_path):
    source = tmp_path / "rows.json"
    output = tmp_path / "labels.parquet"
    summary_path = tmp_path / "summary.json"
    source.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "forward_outcome": {
                            "exit_reason": "target_1",
                            "net_bps": 9.0,
                            "mfe_bps": 12.0,
                            "planned_target_bps": 10.0,
                        }
                    }
                ]
            }
        )
    )

    summary = run(source, output, summary_path)

    assert summary["resolved_outcomes"] == 1
    assert pd.read_parquet(output).loc[0, "tb_label"] == 1
    assert json.loads(summary_path.read_text())["positive_labels"] == 1
