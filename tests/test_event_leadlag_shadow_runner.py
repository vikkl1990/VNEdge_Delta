"""Event lead-lag shadow runner."""

import json
from datetime import UTC

import pandas as pd

from vnedge.research.event_leadlag_alpha import LeadLagFilter, LeadLagMinerConfig
from vnedge.runtime import event_leadlag_shadow_runner as runner
from vnedge.runtime.event_leadlag_shadow_runner import EventLeadLagShadowSpec, ShadowCycleConfig


def candles(*, event_latest: bool, event_offset: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    n = 180
    timestamps = pd.date_range("2026-07-07T00:00:00Z", periods=n, freq="1min")
    leader = [100.0]
    follower = [100.0]
    leader_volume = [100.0] * n
    follower_volume = [100.0] * n
    for i in range(1, n):
        noise = 1.0 + (0.00015 if i % 2 == 0 else -0.00010)
        leader.append(leader[-1] * noise)
        follower.append(follower[-1] * (1.0 + (0.00005 if i % 2 == 0 else -0.00004)))
    if event_latest:
        event_idx = len(leader) - 1 - event_offset
        leader[event_idx] = leader[event_idx - 1] * 1.010
        follower[event_idx] = follower[event_idx - 1] * 1.0001
        leader_volume[event_idx] = 900.0

    def frame(prices, volumes):
        opens = [prices[0], *prices[:-1]]
        return pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": opens,
                "high": [max(o, c) * 1.0001 for o, c in zip(opens, prices)],
                "low": [min(o, c) * 0.9999 for o, c in zip(opens, prices)],
                "close": prices,
                "volume": volumes,
            }
        )

    return frame(leader, leader_volume), frame(follower, follower_volume)


def spec() -> EventLeadLagShadowSpec:
    return EventLeadLagShadowSpec(
        spec_id="sol_binanceusdm_to_delta_india_long_15m_test",
        leader_exchange="binanceusdm",
        leader_symbol="SOL/USDT:USDT",
        follower_exchange="delta_india",
        follower_symbol="SOL/USD:USD",
        side="long",
        horizon_min=15,
        event_filter=LeadLagFilter(20.0, 1.0, -1.0, 0.50, 5.0),
    )


def config() -> ShadowCycleConfig:
    return ShadowCycleConfig(
        miner=LeadLagMinerConfig(
            timeframe="1m",
            lookback_days=5,
            rolling_window=30,
            horizons_min=(15,),
        ),
        max_data_age_minutes=5,
        scan_lookback_minutes=5,
    )


def patch_lanes(
    monkeypatch,
    *,
    event_latest: bool,
    event_offset: int = 0,
) -> pd.Timestamp:
    leader, follower = candles(event_latest=event_latest, event_offset=event_offset)
    frames = {
        ("binanceusdm", "SOL/USDT:USDT", "1m"): leader,
        ("delta_india", "SOL/USD:USD", "1m"): follower,
    }

    class FakeStore:
        def __init__(self, _root):
            pass

        def read_candles(self, exchange, symbol, timeframe):
            return frames[(exchange, symbol, timeframe)]

    monkeypatch.setattr(runner, "ParquetStore", FakeStore)
    return leader["timestamp"].iloc[-1]


def journal_records(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_event_leadlag_shadow_runner_journals_shadow_intent(tmp_path, monkeypatch):
    latest_ts = patch_lanes(monkeypatch, event_latest=True)
    journal = tmp_path / "logs" / "event_leadlag_shadow.jsonl"
    out = tmp_path / "latest.json"

    payload = runner.run_shadow_cycle(
        tmp_path,
        specs=(spec(),),
        config=config(),
        journal_path=journal,
        out_path=out,
        now=latest_ts.to_pydatetime().astimezone(UTC),
    )

    assert payload["summary"]["shadow_intents"] == 1
    assert payload["summary"]["missed_opportunities"] == 0
    intent = payload["shadow_intents"][0]
    assert intent["intent"]["exchange"] == "delta_india"
    assert intent["intent"]["symbol"] == "SOL/USD:USD"
    assert intent["intent"]["order_type"] == "limit"
    assert intent["route_decision"] == "MAKER_ONLY"
    assert intent["approval_state"] == "RESEARCH_SHADOW_ONLY"
    assert intent["requires_l2_replay"] is True

    kinds = [row["kind"] for row in journal_records(journal)]
    assert kinds == ["event_leadlag_eval", "shadow_intent"]
    saved = json.loads(out.read_text())
    assert saved["summary"]["shadow_intents"] == 1


def test_event_leadlag_shadow_runner_logs_why_no_trade(tmp_path, monkeypatch):
    latest_ts = patch_lanes(monkeypatch, event_latest=False)
    journal = tmp_path / "logs" / "event_leadlag_shadow.jsonl"

    payload = runner.run_shadow_cycle(
        tmp_path,
        specs=(spec(),),
        config=config(),
        journal_path=journal,
        now=latest_ts.to_pydatetime().astimezone(UTC),
    )

    assert payload["summary"]["shadow_intents"] == 0
    assert payload["summary"]["missed_opportunities"] == 1
    evaluation = payload["evaluations"][0]
    assert evaluation["state"] == "NO_TRADE"
    assert evaluation["why_no_trade"]
    assert any("leader_move_below" in reason for reason in evaluation["why_no_trade"])
    records = journal_records(journal)
    assert [row["kind"] for row in records] == ["event_leadlag_eval"]
    assert records[0]["payload"]["missed_opportunity"]["logged"] is True


def test_event_leadlag_shadow_runner_scans_recent_event_window(tmp_path, monkeypatch):
    latest_ts = patch_lanes(monkeypatch, event_latest=True, event_offset=2)
    journal = tmp_path / "logs" / "event_leadlag_shadow.jsonl"

    payload = runner.run_shadow_cycle(
        tmp_path,
        specs=(spec(),),
        config=config(),
        journal_path=journal,
        now=latest_ts.to_pydatetime().astimezone(UTC),
    )

    assert payload["summary"]["shadow_intents"] == 1
    evaluation = payload["evaluations"][0]
    assert evaluation["state"] == "SHADOW_INTENT"
    assert evaluation["matched_event_offset_minutes"] == 2.0
    assert evaluation["scan_rows_checked"] >= 3


def test_event_leadlag_shadow_runner_suppresses_duplicate_intent(tmp_path, monkeypatch):
    latest_ts = patch_lanes(monkeypatch, event_latest=True)
    journal = tmp_path / "logs" / "event_leadlag_shadow.jsonl"
    now = latest_ts.to_pydatetime().astimezone(UTC)

    first = runner.run_shadow_cycle(
        tmp_path,
        specs=(spec(),),
        config=config(),
        journal_path=journal,
        now=now,
    )
    second = runner.run_shadow_cycle(
        tmp_path,
        specs=(spec(),),
        config=config(),
        journal_path=journal,
        now=now,
    )

    assert first["summary"]["shadow_intents"] == 1
    assert second["summary"]["shadow_intents"] == 0
    assert second["summary"]["states"] == {"DUPLICATE_SUPPRESSED": 1}
    assert any(
        "duplicate_intent_key" in reason
        for reason in second["evaluations"][0]["why_no_trade"]
    )
    assert [row["kind"] for row in journal_records(journal)] == [
        "event_leadlag_eval",
        "shadow_intent",
        "event_leadlag_eval",
    ]
