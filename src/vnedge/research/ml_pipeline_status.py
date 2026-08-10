"""ML pipeline status — makes the ML integration VISIBLE and honest.

The ML foundation (validation, robustness, features, dataset builder) is built
but role ① (meta-labeling) is data-gated: it needs the paper trials' realized
outcomes as labels. This job answers, on a cadence, "where is ML actually?" so
the dashboard can show it emerging instead of it being invisible backend code.

It runs the real meta-label dataset builder over the live decision journals +
candles, so the training-set count GROWS as the trials mature — that growing
number is the honest signal that ML is progressing. No model is trained here;
this only measures readiness against the locked promotion gates.

Run periodically:  python -m vnedge.research.ml_pipeline_status --interval-seconds 300
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from vnedge.ml.feature_matrix import FEATURE_COLUMNS, FeatureParams, build_feature_matrix  # noqa: F401
from vnedge.ml.meta_label_dataset import build_meta_label_dataset, load_lane_journal_trades

#: labels needed before role ① can honestly train (>= this, then validate).
MIN_LABELS_TO_TRAIN = 200

#: locked promotion gates (mirror docs/ML_INTEGRATION_PLAN.md — pre-registered).
PROMOTION_GATES = {
    "deflated_sharpe_min": 0.95,
    "pbo_max": 0.20,
    "cpcv_median_profit_factor_min": 1.3,
    "must_beat_rule_based_baseline": True,
    "then": "pre-registered untouched-window judgment -> shadow -> paper -> ladder",
}

_ROLE_ORDER = ("meta_labeling", "regime_permission", "standalone_direction")


def _load_candles(data_root: Path) -> dict:
    """Load normalized candle frames keyed by the ccxt symbol the journals use.

    Covers every venue so binance/bybit (USDT-margined) and Delta (USD) trades
    all resolve. This is the *fallback* source, keyed by symbol only — one
    timeframe per symbol (first frame wins). It cannot align a coarse-timeframe
    entry (a 4h trade) to a fine-timeframe frame, so per-lane caches (below) are
    preferred; the lake covers trades from retired lanes that have no cache.

    ``rglob`` finds the parquet wherever the store nests it (``normalized/`` and
    ``exchange=…`` are both valid roots), so callers can pass either ``data`` or
    ``data/normalized`` without silently loading nothing.
    """
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover
        return {}
    paths: dict = {}
    for path in data_root.rglob("symbol=*/timeframe=*/candles.parquet"):
        parts = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in path.parts if "=" in p}
        raw = parts.get("symbol", "")
        if raw.endswith("USDT"):
            symbol = f"{raw[:-4]}/USDT:USDT"   # ETHUSDT -> ETH/USDT:USDT
        elif raw.endswith("USD"):
            symbol = f"{raw[:-3]}/USD:USD"      # ETHUSD  -> ETH/USD:USD
        else:
            continue
        paths.setdefault(symbol, path)
    frames = {}
    for symbol, path in paths.items():
        try:
            frames[symbol] = pd.read_parquet(path)
        except Exception:  # noqa: BLE001
            continue
    return frames


def _load_lane_candles(lane_dir: Path) -> dict:
    """Load each lane's own warmup candle cache, keyed by lane id.

    ``<lane_id>.candles.parquet`` holds the exact symbol *and* timeframe the lane
    trades, so a trade tagged with that lane id joins features at the right bar —
    which one-timeframe-per-symbol candles cannot do. Missing/unreadable caches
    are skipped (the symbol lake remains the fallback); never fatal.
    """
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover
        return {}
    frames: dict = {}
    for path in lane_dir.glob("*.candles.parquet"):
        lane_id = path.name[: -len(".candles.parquet")]
        try:
            frames[lane_id] = pd.read_parquet(path)
        except Exception:  # noqa: BLE001
            continue
    return frames


def build_ml_pipeline_status(*, lane_dir: Path, data_root: Path) -> dict:
    """Assemble the current, honest ML pipeline state."""
    trades = load_lane_journal_trades(lane_dir)
    candles = _load_candles(data_root)
    lane_candles = _load_lane_candles(lane_dir)
    frame = None
    if candles or lane_candles:
        frame, summary = build_meta_label_dataset(
            trades, candles, candles_by_lane=lane_candles, params=FeatureParams()
        )
    else:
        summary = {"samples": 0, "win_rate": 0.0, "by_strategy": {}}

    samples = int(summary.get("samples", 0))

    # Run the real gated meta-labeling harness over the built dataset. It
    # degrades honestly (COLLECTING/TRAINABLE) below the data floor and only
    # trains + validates through the locked DSR/PBO/CPCV gates once there's
    # enough. Never fatal — the status job must publish even if training errors.
    validation = None
    if frame is not None and samples > 0:
        try:
            from vnedge.ml.meta_labeler import evaluate_meta_labeler

            validation = evaluate_meta_labeler(frame).to_dict()
        except Exception as exc:  # noqa: BLE001 — observability must not crash
            validation = {"status": "ERROR", "reason": str(exc)[:200], "passed": False}

    v_status = (validation or {}).get("status", "")
    trainable = bool((validation or {}).get("trainable"))
    validated = v_status in ("VALIDATED_PASS", "VALIDATED_FAIL")
    passed = bool((validation or {}).get("passed"))
    stage = "COLLECTING_LABELS" if samples < MIN_LABELS_TO_TRAIN else "READY_TO_TRAIN"

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "active_role": _ROLE_ORDER[0],
        "role_order": list(_ROLE_ORDER),
        "stage": stage,
        "stages": [
            {"key": "FOUNDATION", "label": "Foundation", "done": True,
             "detail": "validation · robustness · features · dataset builder"},
            {"key": "COLLECTING_LABELS", "label": "Collecting labels", "done": samples >= MIN_LABELS_TO_TRAIN,
             "detail": f"{samples} / {MIN_LABELS_TO_TRAIN} labeled trades", "active": stage == "COLLECTING_LABELS"},
            {"key": "TRAIN", "label": "Train + calibrate", "done": validated,
             "detail": "HistGradientBoosting + isotonic",
             "active": trainable and not validated},
            {"key": "VALIDATE", "label": "Validate (DSR/PBO)", "done": passed,
             "detail": "must clear the locked gates", "active": validated and not passed},
            {"key": "SHADOW", "label": "Shadow vs baseline", "done": False,
             "detail": "beat the rule-based baseline OOS", "active": passed},
            {"key": "PAPER", "label": "Paper → ladder", "done": False,
             "detail": "untouched judgment first"},
        ],
        "dataset": {
            "samples": samples,
            "win_rate_pct": round(float(summary.get("win_rate", 0.0)) * 100, 1),
            "min_to_train": MIN_LABELS_TO_TRAIN,
            "progress_pct": round(min(100.0, samples / MIN_LABELS_TO_TRAIN * 100), 1),
            "by_strategy": summary.get("by_strategy", {}),
        },
        "foundation": {
            "validation": True,
            "robustness": True,
            "feature_count": len(FEATURE_COLUMNS),
            "dataset_builder": True,
        },
        "gates": PROMOTION_GATES,
        # The live gated verdict from the meta-labeling harness (null until there
        # are any labels). Shows the real CPCV PF / DSR / PBO / beats-baseline
        # checks once the dataset clears the CPCV floor.
        "validation": validation,
        "model": None,   # role ① not trained yet — honest null until data + validation
        "can_trade": False,
        "can_promote": False,
        "policy": "models trade ONLY via MLStrategy/gateway/registry; judgment on untouched windows only",
        "note": (
            "Meta-labeling is DATA-GATED: it trains once enough primary-signal "
            "outcomes accumulate from the paper trials (4h fires ~2/week). The "
            "count above is the real, growing training set — nothing is trained "
            "or promoted until it clears the locked gates on untouched data."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lane-dir", default="logs/paper_trials")
    ap.add_argument("--data-root", default="data/normalized")
    ap.add_argument("--output", default="research/live_research/ml_pipeline_status.json")
    ap.add_argument("--interval-seconds", type=float, default=0.0)
    args = ap.parse_args(argv)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    def once() -> None:
        status = build_ml_pipeline_status(
            lane_dir=Path(args.lane_dir), data_root=Path(args.data_root)
        )
        out.write_text(json.dumps(status, indent=2))
        print(f"ml_pipeline_status: stage={status['stage']} samples={status['dataset']['samples']}", flush=True)

    once()
    while args.interval_seconds > 0:
        time.sleep(args.interval_seconds)
        try:
            once()
        except Exception as exc:  # noqa: BLE001 - a status job must not crash the fleet
            print(f"ml_pipeline_status error: {exc!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
