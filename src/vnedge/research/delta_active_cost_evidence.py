"""Reprice the frozen Delta trade set under the currently loaded fee model.

This closes an evidence gap in the dashboard: aggregate net bps can be shifted
between fee scenarios, but profit factor cannot.  The calculation therefore
uses the preserved per-trade gross return and reconstructs each trade's cost
from the same ``DeltaFeeModel`` used by the research engine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd

from vnedge.scalping.delta_engine.config import load_delta_scalper_config
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel


def _profit_factor(values: pd.Series) -> float | None:
    gains = float(values[values > 0].sum())
    losses = abs(float(values[values < 0].sum()))
    if losses:
        return gains / losses
    return None if gains else 0.0


def build_active_cost_evidence(
    trades_path: Path,
    *,
    config_path: Path = Path("configs/delta_scalper.yaml"),
) -> dict[str, Any]:
    config = load_delta_scalper_config(config_path)
    settings = config.fee_model
    fee_model = DeltaFeeModel(**settings.model_dump())
    frame = pd.read_parquet(trades_path)
    required = {"symbol", "gross_bps", "entry_is_maker", "hold_seconds"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError("trade evidence is missing columns: " + ", ".join(missing))
    frame = frame.dropna(subset=list(required)).copy()
    if frame.empty:
        raise ValueError("trade evidence contains no complete trades")
    frame["active_cost_bps"] = [
        fee_model.breakdown(
            str(row.symbol),
            entry_is_maker=bool(row.entry_is_maker),
            exit_is_maker=False,
            hold_seconds=float(row.hold_seconds),
        ).total_bps
        for row in frame.itertuples(index=False)
    ]
    frame["active_net_bps"] = frame["gross_bps"].astype(float) - frame[
        "active_cost_bps"
    ]

    def metrics(group: pd.DataFrame) -> dict[str, Any]:
        values = group["active_net_bps"]
        return {
            "trades": int(len(group)),
            "gross_bps": float(group["gross_bps"].sum()),
            "cost_bps": float(group["active_cost_bps"].sum()),
            "net_bps": float(values.sum()),
            "average_gross_bps": float(group["gross_bps"].mean()),
            "average_cost_bps": float(group["active_cost_bps"].mean()),
            "average_net_bps": float(values.mean()),
            "profit_factor": _profit_factor(values),
            "win_rate": float((values > 0).mean()),
        }

    market_metrics = {
        str(symbol): metrics(group)
        for symbol, group in frame.groupby("symbol", sort=True)
    }
    source_hash = hashlib.sha256(trades_path.read_bytes()).hexdigest()
    return {
        "schema_version": "vnedge.delta_active_cost_evidence.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source": str(trades_path),
        "source_sha256": source_hash,
        "config": str(config_path),
        "fee_model": settings.model_dump(mode="json"),
        "metrics": metrics(frame),
        "markets": market_metrics,
        "positive_markets": sum(
            row["average_net_bps"] > 0 for row in market_metrics.values()
        ),
        "data_contract": "per-trade gross_bps repriced with current Delta fee model",
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "order_route": "absent",
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trades",
        type=Path,
        default=Path("research/live_research/delta_scalper_with_tb_labels.parquet"),
    )
    parser.add_argument(
        "--config", type=Path, default=Path("configs/delta_scalper.yaml")
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/live_research/delta_active_cost_evidence_latest.json"),
    )
    args = parser.parse_args(argv)
    payload = build_active_cost_evidence(args.trades, config_path=args.config)
    _atomic_json(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
