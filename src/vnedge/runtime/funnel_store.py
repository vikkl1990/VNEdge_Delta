"""Cross-session funnel-counter persistence.

The live-session funnel (bars/evals/signals/…) is held in memory, so every
restart resets it to 0. On a slow-timeframe lane that means the dashboard reads
"0 evals / 0 signals" for hours after each deploy even though the lane is alive
and its journals kept growing — a quiet-by-design system that looks dead. This
store snapshots the counters per lane (atomic tmp+rename, same crash-safety as
the account store) and restores them on the next launch so the funnel RESUMES
instead of resetting.

Display/telemetry ONLY — these counters gate no trading decision (the decision
journal and fill ledger remain the source of truth for anything that matters),
so a missing or corrupt store is never fatal: it just starts the counters at 0.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# The cumulative activity counters worth resuming across restarts.
_COUNTER_FIELDS = (
    "bars_processed",
    "evals",
    "live_evals",
    "backfill_evals",
    "signals",
    "live_signals",
    "backfill_signals",
    "orders_submitted",
    "risk_rejects",
    "sizing_skips",
    "shadow_approved",
    "shadow_rejected",
    "tick_stop_exits",
    "recon_mismatches",
)


class LaneFunnelStore:
    """Persist/restore a lane's funnel counters + last-fired timestamp."""

    def __init__(self, path: Path | str, lane_id: str) -> None:
        self.path = Path(path)
        self.lane_id = lane_id

    def save_from(self, session) -> None:
        state = {
            "lane_id": self.lane_id,
            "saved_at": datetime.now(UTC).isoformat(),
            "counters": {
                field: int(getattr(session, field, 0) or 0) for field in _COUNTER_FIELDS
            },
            "last_fired_ts": getattr(session, "last_fired_ts", None),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, indent=2))
            tmp.replace(self.path)
        except OSError as exc:  # persistence must never take down a lane
            logger.warning("funnel store save failed for %s: %s", self.lane_id, exc)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            state = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("funnel store load failed for %s: %s", self.lane_id, exc)
            return None
        if not isinstance(state, dict) or state.get("lane_id") != self.lane_id:
            # a moved/renamed store belonging to another lane must never bleed
            # its counters into this one — ignore it and start fresh
            logger.warning(
                "funnel store at %s belongs to '%s', not '%s' — ignoring",
                self.path, state.get("lane_id") if isinstance(state, dict) else "?",
                self.lane_id,
            )
            return None
        return state

    def restore_into(self, session) -> bool:
        """Resume the counters onto ``session``. True if prior state existed."""
        state = self.load()
        if state is None:
            return False
        counters = state.get("counters") or {}
        for field in _COUNTER_FIELDS:
            if field in counters:
                try:
                    setattr(session, field, int(counters[field]))
                except (TypeError, ValueError):
                    continue
        last_fired = state.get("last_fired_ts")
        if last_fired:
            session.last_fired_ts = str(last_fired)
        logger.info(
            "resumed funnel for %s: %d evals / %d live signals (saved %s)",
            self.lane_id,
            int(getattr(session, "evals", 0) or 0),
            int(getattr(session, "live_signals", 0) or 0),
            state.get("saved_at"),
        )
        return True
