from __future__ import annotations

import json
from pathlib import Path

import pytest

from vnedge.research.event_edge_attribution import build_event_edge_attribution


def _outcome(index: int, *, gross_ticks: float, symbol: str = "BTCUSD") -> dict:
    cost_ticks = 2.96 if symbol == "BTCUSD" else 29.6
    return {
        "kind": "delta_absorption_research_outcome",
        "payload": {
            "key": f"k{index}",
            "symbol": symbol,
            "decision_ts": "2026-08-12T05:00:00+00:00",
            "entry_price": 1000.0,
            "realized_exit_reason": "target_1" if gross_ticks > 0 else "stop",
            "realized_gross_ticks": gross_ticks,
            "cost_ticks": cost_ticks,
            "realized_net_ticks": gross_ticks - cost_ticks,
            "mfe_ticks": max(gross_ticks, 0),
            "mae_ticks": max(-gross_ticks, 0),
            "hit_target_1": gross_ticks > 0,
            "stopped_out": gross_ticks < 0,
            "was_stacked": False,
            "volume_percentile": 0.5,
            "horizon_returns_bps": {"1000": 20.0},
            "event": {"reversal_direction": 1, "strength": 0.9},
        },
    }


def test_attribution_is_bps_normalized_and_never_authorizes(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        "\n".join(
            json.dumps(_outcome(index, gross_ticks=8 if index % 2 else -6)) for index in range(6)
        )
        + "\n"
    )

    payload = build_event_edge_attribution(
        journal,
        output_path=tmp_path / "result.json",
        minimum_cell_trades=2,
    )

    assert payload["source"]["completed"] == 6
    assert payload["economics"]["average_cost_bps"] == pytest.approx(14.8)
    assert payload["economics"]["average_gross_bps"] == pytest.approx(5.0)
    assert payload["economics"]["average_net_bps"] == pytest.approx(-9.8)
    assert payload["economics"]["cost_clear_mfe_rate"] == 0.5
    assert payload["fixed_horizon_economics"]["1000"]["average_net_bps"] == pytest.approx(5.2)
    assert payload["diagnosis"]["verdict"] == "NO_AFTER_COST_EDGE"
    assert payload["scanner_implementation_authorized"] is False
    assert payload["selection_authorized"] is False
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_sparse_positive_cells_are_excluded(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    rows = [_outcome(index, gross_ticks=-6) for index in range(4)]
    rare = _outcome(99, gross_ticks=100)
    rare["payload"]["was_stacked"] = True
    rows.append(rare)
    journal.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    payload = build_event_edge_attribution(
        journal,
        output_path=tmp_path / "result.json",
        minimum_cell_trades=2,
    )

    assert all(row["trades"] >= 2 for row in payload["interaction_cells"])
    assert payload["diagnosis"]["positive_net_interaction_cells"] == 0


def test_duplicate_outcomes_are_counted_once(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    outcome = _outcome(1, gross_ticks=8)
    journal.write_text(json.dumps(outcome) + "\n" + json.dumps(outcome) + "\n")

    payload = build_event_edge_attribution(
        journal,
        output_path=tmp_path / "result.json",
        minimum_cell_trades=1,
    )

    assert payload["source"]["outcomes"] == 1
    assert payload["source"]["duplicate_outcomes_ignored"] == 1
