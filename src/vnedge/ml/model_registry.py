"""Model registry — versioned, auditable model storage.

Every saved model gets an immutable version id plus a metadata JSON recording
feature names (order matters — it is the model contract), hyperparameters,
label configuration, training window, and whatever evaluation metrics the
caller supplies. Nothing trades a model that isn't in the registry; the
approval workflow references version ids, never pickle files by path.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import joblib

from vnedge.ml.trainer import TrainedModel


class ModelRegistry:
    def __init__(self, root: Path | str = "models") -> None:
        self.root = Path(root)

    def save(self, trained: TrainedModel, metadata: dict) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        digest = hashlib.sha256(
            json.dumps(
                {"features": trained.feature_names, "params": trained.params},
                sort_keys=True, default=str,
            ).encode()
        ).hexdigest()[:8]
        version = f"hgb_{stamp}_{digest}"
        target = self.root / version
        target.mkdir(parents=True, exist_ok=False)

        joblib.dump(trained.model, target / "model.joblib")
        meta = {
            "version": version,
            "created_at": datetime.now(UTC).isoformat(),
            "feature_names": list(trained.feature_names),
            "params": trained.params,
            "train_rows": trained.train_rows,
            "positive_rate": trained.positive_rate,
            "importances": [list(kv) for kv in trained.importances],
            **metadata,
        }
        (target / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
        return version

    def load(self, version: str) -> tuple[TrainedModel, dict]:
        target = self.root / version
        meta = json.loads((target / "meta.json").read_text())
        model = joblib.load(target / "model.joblib")
        trained = TrainedModel(
            model=model,
            feature_names=tuple(meta["feature_names"]),
            params=meta["params"],
            train_rows=meta["train_rows"],
            positive_rate=meta["positive_rate"],
            importances=tuple((n, s) for n, s in meta.get("importances", [])),
        )
        return trained, meta

    def list_versions(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "meta.json").exists())
