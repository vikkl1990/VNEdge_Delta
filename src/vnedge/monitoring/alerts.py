"""Alert rules engine (charter: /monitoring).

Rules evaluate the SAME coalesced state snapshot the dashboard shows — one
source of truth, no separate metrics path. Fired alerts are appended to an
alerts.jsonl (journal-first, like everything here) and fanned out to
notifiers, each individually guarded: a dead Telegram API must never touch
the trading loop.

Rules are plain predicates over the snapshot dict, so AND/OR is just Python.
Cooldowns deduplicate: a stale feed alerts once per cooldown window, not
once per second.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, alert: dict) -> None: ...


@dataclass(frozen=True)
class AlertRule:
    rule_id: str
    severity: str  # "info" | "warning" | "critical"
    condition: Callable[[dict], bool]
    message: Callable[[dict], str]
    cooldown_seconds: float = 3600.0


class AlertEngine:
    def __init__(
        self,
        rules: list[AlertRule],
        store_path: Path | str,
        notifiers: list[Notifier] | None = None,
    ) -> None:
        self.rules = rules
        self.store_path = Path(store_path)
        self.notifiers = notifiers or []
        self._last_fired: dict[str, datetime] = {}
        self.recent: deque[dict] = deque(maxlen=20)  # newest last; dashboard feed

    def evaluate(self, snapshot: dict, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(UTC)
        fired: list[dict] = []
        for rule in self.rules:
            try:
                hit = rule.condition(snapshot)
            except Exception as exc:  # noqa: BLE001 — a broken rule must not kill the loop
                logger.error("alert rule %s raised: %s", rule.rule_id, exc)
                continue
            if not hit:
                continue
            last = self._last_fired.get(rule.rule_id)
            if last is not None and (now - last).total_seconds() < rule.cooldown_seconds:
                continue
            self._last_fired[rule.rule_id] = now
            alert = {
                "ts": now.isoformat(),
                "rule_id": rule.rule_id,
                "severity": rule.severity,
                "message": rule.message(snapshot),
                "mode": snapshot.get("mode"),
            }
            fired.append(alert)
            self.recent.append(alert)
            self._persist(alert)
            for notifier in self.notifiers:
                try:
                    notifier.send(alert)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("notifier %s failed: %s", type(notifier).__name__, exc)
        return fired

    def _persist(self, alert: dict) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.store_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(alert) + "\n")
        except OSError as exc:
            logger.error("could not persist alert: %s", exc)


def _new_fill_rule() -> AlertRule:
    """Stateful via closure: fires when the fill count grows — the operator
    hears about every trade without watching the dashboard."""
    seen = {"fills": 0}

    def changed(s: dict) -> bool:
        fills = int(s.get("fills", 0))
        if fills > seen["fills"]:
            seen["fills"] = fills
            return True
        return False

    return AlertRule(
        "new_fill", "info", changed,
        lambda s: f"fill #{s['fills']} — equity ${s['equity']:.2f}, "
                  f"positions: {len(s['positions'])}",
        cooldown_seconds=0.0,  # every fill matters
    )


def default_trial_rules(daily_loss_limit_usd: float, max_drawdown_pct: float = 6.0) -> list[AlertRule]:
    return [
        AlertRule(
            "feed_stale", "critical",
            lambda s: s["feed_health"]["last_update_ms"] > 120_000,
            lambda s: f"feed stale: {s['feed_health']['last_update_ms'] / 1000:.0f}s since last event",
            cooldown_seconds=1800,
        ),
        AlertRule(
            "kill_switch", "critical",
            lambda s: bool(s["kill_switch_active"]),
            lambda s: "KILL SWITCH ACTIVE — entries blocked, exits only",
            cooldown_seconds=3600,
        ),
        AlertRule(
            "journal_unhealthy", "critical",
            lambda s: s["last_journal_write"] != "ok",
            lambda s: "decision journal unavailable — new risk blocked",
            cooldown_seconds=1800,
        ),
        AlertRule(
            "risk_status", "warning",
            lambda s: s["risk_status"] != "ok" and not s["kill_switch_active"],
            lambda s: f"risk status: {s['risk_status']}",
            cooldown_seconds=1800,
        ),
        AlertRule(
            "daily_loss", "critical",
            lambda s: s["daily_pnl"] <= -daily_loss_limit_usd,
            lambda s: f"daily loss stop: ${s['daily_pnl']:.2f} (limit -${daily_loss_limit_usd:.2f})",
            cooldown_seconds=6 * 3600,
        ),
        AlertRule(
            "loss_streak", "warning",
            lambda s: int(s["consecutive_losses"]) >= 3,
            lambda s: f"{s['consecutive_losses']} consecutive losing round trips",
            cooldown_seconds=6 * 3600,
        ),
        AlertRule(
            "drawdown", "critical",
            lambda s: s.get("peak_equity", 0) > 0
            and (s["peak_equity"] - s["equity"]) / s["peak_equity"] * 100.0 > max_drawdown_pct,
            lambda s: f"drawdown {(s['peak_equity'] - s['equity']) / s['peak_equity'] * 100.0:.1f}% "
                      f"exceeds trial envelope {max_drawdown_pct}%",
            cooldown_seconds=6 * 3600,
        ),
        _new_fill_rule(),
    ]
