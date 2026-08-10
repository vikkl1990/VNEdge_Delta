"""Experiment recorder — an MLflow-style, queryable write store for runs.

The ModelRegistry versions *models*; this versions whole *runs* — the params a
run used, the metrics it produced, and any objects it saved — and makes them
searchable. It is the write side that the read-only ``experiment_index`` and the
scattered JSONL feeds never gave: a research module calls ``log_params`` /
``log_metrics`` / ``log_artifact`` as it goes, and later anyone can
``search_records`` across every run.

Dependency-light and local (no MLflow server): directory-per-run, exactly like
``ModelRegistry`` — ``<root>/<run_id>/meta.json`` plus an ``artifacts/`` dir.
Writes are atomic (tmp + replace). It records; it never trades, promotes, or
gates — provenance discipline still lives in ``data_burn``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable

DEFAULT_ROOT = Path("research/experiments")

STATUS_RUNNING = "RUNNING"
STATUS_FINISHED = "FINISHED"
STATUS_FAILED = "FAILED"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, prefix=path.name, suffix=".tmp",
                            delete=False, encoding="utf-8") as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True, default=str)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


class ExperimentRecorder:
    """Local, queryable store of experiment runs."""

    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)

    # ---------------------------------------------------------------- write
    def start_run(
        self, name: str, *, tags: dict[str, Any] | None = None, now: datetime | None = None
    ) -> str:
        moment = now or datetime.now(UTC)
        stamp = moment.strftime("%Y%m%dT%H%M%S")
        digest = hashlib.sha256(
            json.dumps({"name": name, "tags": tags or {}}, sort_keys=True, default=str).encode()
        ).hexdigest()[:8]
        run_id = f"run_{stamp}_{digest}"
        meta = {
            "run_id": run_id,
            "name": name,
            "created_at": moment.isoformat(),
            "status": STATUS_RUNNING,
            "tags": tags or {},
            "params": {},
            "metrics": {},
            "artifacts": [],
        }
        target = self.root / run_id
        target.mkdir(parents=True, exist_ok=False)  # collision = hard error
        _atomic_write_json(target / "meta.json", meta)
        return run_id

    def _update(self, run_id: str, mutate: Callable[[dict], None]) -> None:
        meta_path = self.root / run_id / "meta.json"
        if not meta_path.exists():
            raise KeyError(f"unknown run {run_id!r}")
        meta = json.loads(meta_path.read_text())
        mutate(meta)
        meta["updated_at"] = datetime.now(UTC).isoformat()
        _atomic_write_json(meta_path, meta)

    def log_params(self, run_id: str, params: dict[str, Any]) -> None:
        """Params are set-once inputs; merged into the run."""
        self._update(run_id, lambda m: m["params"].update(params))

    def log_metrics(self, run_id: str, metrics: dict[str, Any]) -> None:
        """Metrics are outputs; latest value per key wins (merged)."""
        self._update(run_id, lambda m: m["metrics"].update(metrics))

    def log_artifact(self, run_id: str, name: str, data: str | bytes) -> Path:
        """Save an object/blob under the run's artifacts/ dir; returns its path."""
        art_dir = self.root / run_id / "artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        path = art_dir / name
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data)
        self._update(run_id, lambda m: m["artifacts"].append(name) if name not in m["artifacts"] else None)
        return path

    def set_status(self, run_id: str, status: str) -> None:
        self._update(run_id, lambda m: m.__setitem__("status", status))

    # ----------------------------------------------------------------- read
    def get_run(self, run_id: str) -> dict:
        meta_path = self.root / run_id / "meta.json"
        if not meta_path.exists():
            raise KeyError(f"unknown run {run_id!r}")
        return json.loads(meta_path.read_text())

    def runs(self) -> list[dict]:
        if not self.root.exists():
            return []
        out = []
        for d in sorted(self.root.iterdir()):
            meta = d / "meta.json"
            if meta.exists():
                out.append(json.loads(meta.read_text()))
        out.sort(key=lambda m: m.get("created_at", ""))
        return out

    def search_records(
        self,
        *,
        name: str | None = None,
        tag: tuple[str, Any] | None = None,
        status: str | None = None,
        where: Callable[[dict], bool] | None = None,
    ) -> list[dict]:
        """Query runs. ``where`` receives the full run dict (params/metrics/tags)
        for arbitrary predicates, e.g. ``where=lambda r: r['metrics'].get('pf',0) > 1.2``."""
        rows = self.runs()
        if name is not None:
            rows = [r for r in rows if r.get("name") == name]
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        if tag is not None:
            k, v = tag
            rows = [r for r in rows if r.get("tags", {}).get(k) == v]
        if where is not None:
            rows = [r for r in rows if where(r)]
        return rows

    # ------------------------------------------------------------ ergonomics
    def run(self, name: str, *, tags: dict[str, Any] | None = None) -> "RunHandle":
        """Context manager: FINISHED on clean exit, FAILED if the block raises."""
        return RunHandle(self, self.start_run(name, tags=tags))


@dataclass
class RunHandle:
    recorder: ExperimentRecorder
    run_id: str

    def log_params(self, params: dict[str, Any]) -> None:
        self.recorder.log_params(self.run_id, params)

    def log_metrics(self, metrics: dict[str, Any]) -> None:
        self.recorder.log_metrics(self.run_id, metrics)

    def log_artifact(self, name: str, data: str | bytes) -> Path:
        return self.recorder.log_artifact(self.run_id, name, data)

    def __enter__(self) -> "RunHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.recorder.set_status(self.run_id, STATUS_FAILED if exc_type else STATUS_FINISHED)
