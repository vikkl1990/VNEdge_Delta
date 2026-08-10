"""Alert rules engine — conditions, cooldown, persistence, guarded notifiers."""

import json
from datetime import UTC, datetime, timedelta

from vnedge.monitoring.alerts import AlertEngine, AlertRule, default_trial_rules

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


def snapshot(**overrides) -> dict:
    base = {
        "mode": "paper (live data)",
        "kill_switch_active": False,
        "equity": 500.0,
        "peak_equity": 500.0,
        "daily_pnl": 0.0,
        "consecutive_losses": 0,
        "risk_status": "ok",
        "last_journal_write": "ok",
        "fills": 0,
        "positions": [],
        "feed_health": {"last_update_ms": 500.0},
    }
    base.update(overrides)
    return base


def engine(tmp_path, rules=None, notifiers=None) -> AlertEngine:
    return AlertEngine(
        rules if rules is not None else default_trial_rules(10.0),
        tmp_path / "alerts.jsonl", notifiers,
    )


def test_healthy_snapshot_fires_nothing(tmp_path):
    assert engine(tmp_path).evaluate(snapshot(), NOW) == []


def test_stale_feed_fires_critical(tmp_path):
    fired = engine(tmp_path).evaluate(
        snapshot(feed_health={"last_update_ms": 300_000.0}), NOW
    )
    assert [a["rule_id"] for a in fired] == ["feed_stale"]
    assert fired[0]["severity"] == "critical"


def test_daily_loss_and_streak_fire_together(tmp_path):
    fired = engine(tmp_path).evaluate(
        snapshot(daily_pnl=-11.0, consecutive_losses=4), NOW
    )
    assert {a["rule_id"] for a in fired} == {"daily_loss", "loss_streak"}


def test_drawdown_rule(tmp_path):
    fired = engine(tmp_path).evaluate(
        snapshot(equity=460.0, peak_equity=500.0), NOW  # 8% > 6% envelope
    )
    assert any(a["rule_id"] == "drawdown" for a in fired)


def test_cooldown_deduplicates(tmp_path):
    e = engine(tmp_path)
    stale = snapshot(feed_health={"last_update_ms": 300_000.0})
    assert len(e.evaluate(stale, NOW)) == 1
    assert e.evaluate(stale, NOW + timedelta(minutes=5)) == []  # inside cooldown
    assert len(e.evaluate(stale, NOW + timedelta(hours=1))) == 1  # cooldown over


def test_new_fill_rule_fires_per_fill_increase(tmp_path):
    e = engine(tmp_path)
    assert any(a["rule_id"] == "new_fill" for a in e.evaluate(snapshot(fills=1), NOW))
    assert e.evaluate(snapshot(fills=1), NOW) == []  # unchanged count: silent
    assert any(a["rule_id"] == "new_fill" for a in e.evaluate(snapshot(fills=2), NOW))


def test_alerts_persisted_to_jsonl(tmp_path):
    e = engine(tmp_path)
    e.evaluate(snapshot(kill_switch_active=True), NOW)
    lines = (tmp_path / "alerts.jsonl").read_text().strip().splitlines()
    record = json.loads(lines[0])
    assert record["rule_id"] == "kill_switch"
    assert "KILL SWITCH" in record["message"]


def test_broken_rule_and_notifier_never_raise(tmp_path):
    class ExplodingNotifier:
        def send(self, alert):
            raise RuntimeError("telegram down")

    rules = [
        AlertRule("broken", "info", lambda s: s["nope"], lambda s: "x"),
        AlertRule("ok", "info", lambda s: True, lambda s: "fine"),
    ]
    e = engine(tmp_path, rules=rules, notifiers=[ExplodingNotifier()])
    fired = e.evaluate(snapshot(), NOW)  # must not raise
    assert [a["rule_id"] for a in fired] == ["ok"]
