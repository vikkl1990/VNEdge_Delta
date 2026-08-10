"""Holdout-manifest loading for replay window governance."""

from __future__ import annotations

import json
from pathlib import Path

from vnedge.replay.models import HoldoutManifest, ReplayWindow


def load_holdout_manifest(path: Path | str) -> HoldoutManifest:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "vnedge.event_replay_holdout.v1":
        raise ValueError("unsupported replay holdout manifest schema")

    def windows(key: str) -> tuple[ReplayWindow, ...]:
        raw = payload.get(key, [])
        if not isinstance(raw, list):
            raise TypeError(f"holdout manifest {key} must be a list")
        return tuple(
            ReplayWindow(
                name=str(row["name"]),
                start_ts_us=int(row["start_ts_us"]),
                end_ts_us=int(row["end_ts_us"]),
                symbols=tuple(row.get("symbols", ())),
            )
            for row in raw
        )

    return HoldoutManifest(
        development_windows=windows("development_windows"),
        sealed_windows=windows("sealed_windows"),
        source=source,
    )
