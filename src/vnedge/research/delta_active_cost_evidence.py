"""Reprice the frozen Delta trade set under one canonical cost contract.

This closes an evidence gap in the dashboard: aggregate net bps can be shifted
between fee scenarios, but profit factor cannot.  The calculation therefore
uses preserved per-trade gross return and the registry's declared route cost.
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

from vnedge.research.strategy_evidence_registry import route_cost_contract
from vnedge.scalping.delta_engine.config import load_delta_scalper_config


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
    cost_contract_id: str = "taker_full_14_8",
) -> dict[str, Any]:
    config = load_delta_scalper_config(config_path)
    settings = config.fee_model
    contract = route_cost_contract(cost_contract_id)
    frame = pd.read_parquet(trades_path)
    required = {"symbol", "gross_bps", "entry_is_maker", "hold_seconds"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError("trade evidence is missing columns: " + ", ".join(missing))
    frame = frame.dropna(subset=list(required)).copy()
    if frame.empty:
        raise ValueError("trade evidence contains no complete trades")
    # The route contract, not the scanner's requested order style, is the
    # authority for evidence pricing. A next-bar-open fill cannot prove maker
    # execution, so the conservative canonical result intentionally ignores
    # ``entry_is_maker`` when the declared contract is taker/taker. The source
    # flag remains in diagnostics so optimistic historical assumptions stay
    # visible rather than silently changing the economic contract.
    frame["active_cost_bps"] = contract.total_roundtrip_bps
    frame["active_net_bps"] = frame["gross_bps"].astype(float) - frame[
        "active_cost_bps"
    ]

    def metrics(group: pd.DataFrame) -> dict[str, Any]:
        values = group["active_net_bps"]
        return {
            "trades": len(group),
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
        "cost_contract": cost_contract_id,
        "route_cost_contract": contract.to_dict(),
        "source_route_diagnostics": {
            "trades_marked_maker_entry": int(frame["entry_is_maker"].astype(bool).sum()),
            "trades_marked_taker_entry": int((~frame["entry_is_maker"].astype(bool)).sum()),
            "source_entry_route_used_for_pricing": False,
        },
        "metrics": metrics(frame),
        "markets": market_metrics,
        "positive_markets": sum(
            row["average_net_bps"] > 0 for row in market_metrics.values()
        ),
        "data_contract": (
            "per-trade gross_bps repriced with one canonical route-cost contract; "
            "source maker/taker flags are diagnostic only"
        ),
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
    parser.add_argument("--cost-contract", default="taker_full_14_8")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/live_research/delta_active_cost_evidence_latest.json"),
    )
    args = parser.parse_args(argv)
    payload = build_active_cost_evidence(
        args.trades,
        config_path=args.config,
        cost_contract_id=args.cost_contract,
    )
    _atomic_json(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
