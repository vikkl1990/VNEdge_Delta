"""Active Delta fee evidence is recomputed from per-trade gross returns."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from vnedge.research.delta_active_cost_evidence import build_active_cost_evidence


def test_active_cost_evidence_reprices_every_trade_and_recomputes_pf(
    tmp_path: Path,
) -> None:
    source = tmp_path / "trades.parquet"
    pd.DataFrame(
        [
            {
                "symbol": "BTCUSD",
                "gross_bps": 20.0,
                "entry_is_maker": False,
                "hold_seconds": 1_900.0,
            },
            {
                "symbol": "ETHUSD",
                "gross_bps": 5.0,
                "entry_is_maker": False,
                "hold_seconds": 1_900.0,
            },
        ]
    ).to_parquet(source, index=False)

    result = build_active_cost_evidence(
        source, config_path=Path("configs/delta_scalper.yaml")
    )

    # Active config: 5 bps taker each side + GST, plus 1.5 bps slippage each leg.
    assert result["metrics"]["average_cost_bps"] == pytest.approx(14.8)
    assert result["metrics"]["net_bps"] == pytest.approx(-4.6)
    assert result["metrics"]["profit_factor"] == pytest.approx(5.2 / 9.8)
    assert result["positive_markets"] == 1
    assert result["can_trade"] is False
    assert result["can_promote"] is False


def test_active_cost_evidence_rejects_incomplete_trade_contract(tmp_path: Path) -> None:
    source = tmp_path / "bad.parquet"
    pd.DataFrame([{"symbol": "BTCUSD", "gross_bps": 10.0}]).to_parquet(
        source, index=False
    )

    with pytest.raises(ValueError, match="entry_is_maker"):
        build_active_cost_evidence(source)
