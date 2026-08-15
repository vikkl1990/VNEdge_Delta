"""Causality, quality scoring, controls, and safety tests for event episodes."""

from datetime import UTC, datetime

from vnedge.research.event_direction_response import ResponseSnapshot
from vnedge.research.event_episode_quality import (
    EventEpisodeQualityConfig,
    QualityDetection,
    build_event_episode_quality,
    collapse_quality_episodes,
)
from vnedge.research.event_response_atlas import PriceTape


def _us(hour: int = 1) -> int:
    return int(datetime(2026, 1, 1, hour, tzinfo=UTC).timestamp() * 1_000_000)


def _detection(key: str, decision_us: int, *, direction: int = 1) -> QualityDetection:
    return QualityDetection(
        key=key,
        symbol="ETHUSD",
        decision_ts_us=decision_us,
        price=100.0,
        reversal_direction=direction,
        aggressive_notional_usd=30_000.0,
        dynamic_minimum_notional_usd=10_000.0,
        price_range_ticks=0.0,
        absorption_ratio=1.0,
        resting_hold_ratio=0.9,
        replenishment_ratio=0.8,
        refresh_count=3,
        strength=0.9,
        volume_percentile=1.0,
        stacked=False,
    )


def _snapshot(key: str, delay_ms: int, *, oi: bool = True) -> ResponseSnapshot:
    requested = _us() + delay_ms * 1_000
    return ResponseSnapshot(
        event_key=key,
        symbol="ETHUSD",
        response_delay_ms=delay_ms,
        requested_ts_us=requested,
        captured_ts_us=requested,
        last_trade_price=100.10,
        mid=100.10,
        spread_bps=1.0,
        book_imbalance=0.5,
        flow_imbalance=0.8,
        aggressive_buy_usd=50_000.0,
        aggressive_sell_usd=2_000.0,
        open_interest=1_000.0 if oi else None,
        open_interest_delta=1.0 if oi else None,
        basis_bps=5.0,
        market_truth_ready=True,
        market_truth_blockers=(),
        trade_book_join_ok=True,
    )


def _tape() -> PriceTape:
    event = _us()
    quality = event + 3_000_000
    control = quality + 1_800_000_000
    timestamps = (
        event - 300_000_000,
        event - 1_000_000,
        event + 250_000,
        quality,
        quality + 250_000,
        quality + 900_250_000,
        control - 300_000_000,
        control,
        control + 250_000,
        control + 900_250_000,
    )
    prices = (100.0, 100.0, 100.1, 100.1, 100.1, 100.5, 100.4, 100.5, 100.5, 100.55)
    return PriceTape(timestamps, prices)


def test_episode_collapse_keeps_first_causal_anchor_and_counts_late_duplicates():
    start = _us()
    rows = (
        _detection("first", start),
        _detection("inside", start + 2_000_000),
        _detection("late_duplicate", start + 10_000_000, direction=-1),
        _detection("new", start + 50_000_000),
    )

    episodes, report = collapse_quality_episodes(
        rows,
        separation_ms=30_000,
        quality_window_ms=3_000,
    )

    assert [row.key for row in episodes] == ["first", "new"]
    assert episodes[0].raw_detection_count == 3
    assert episodes[0].causal_detection_count == 2
    assert episodes[0].direction_conflict is False
    assert report["collapsed_detections"] == 2


def test_quality_engine_enforces_full_funnel_and_never_authorizes_capital(tmp_path):
    detection = _detection("event-1", _us())
    snapshots = {
        (detection.key, delay): _snapshot(detection.key, delay) for delay in (250, 1_000, 3_000)
    }
    result = build_event_episode_quality(
        tmp_path / "unused.jsonl",
        output_path=tmp_path / "quality.json",
        detections=(detection,),
        snapshots=snapshots,
        tapes={"ETHUSD": _tape()},
        config=EventEpisodeQualityConfig(
            minimum_control_pairs=1,
            control_offsets_ms=(1_800_000,),
        ),
        tick_sizes={"ETHUSD": 0.01},
        code_version="test",
    )

    funnel = {row["id"]: row for row in result["funnel"]}
    assert funnel["raw_events"]["value"] == 1
    assert funnel["independent_episodes"]["value"] == 1
    assert funnel["market_truth_complete"]["value"] == 1
    assert funnel["abnormal_vs_control"]["value"] == 1
    assert funnel["directional_confirmation"]["value"] == 1
    assert funnel["cleared_fee_wall"]["value"] == 1
    assert funnel["simulated_outcome"]["value"] == 0
    assert result["control_qualification"]["passed"] is True
    assert result["control_qualification"]["all_matched_pairs"] == 1
    assert result["can_trade"] is False
    assert result["can_promote"] is False
    assert result["paper_authorized"] is False
    assert result["order_route"] == "absent"


def test_missing_oi_dislocation_fails_market_truth_closed(tmp_path):
    detection = _detection("event-1", _us())
    snapshots = {
        (detection.key, delay): _snapshot(detection.key, delay, oi=False)
        for delay in (250, 1_000, 3_000)
    }
    result = build_event_episode_quality(
        tmp_path / "unused.jsonl",
        output_path=tmp_path / "quality.json",
        detections=(detection,),
        snapshots=snapshots,
        tapes={"ETHUSD": _tape()},
        config=EventEpisodeQualityConfig(
            minimum_control_pairs=1,
            control_offsets_ms=(1_800_000,),
        ),
        tick_sizes={"ETHUSD": 0.01},
        code_version="test",
    )

    episode = result["episodes"][0]
    assert episode["anchor_price"] == detection.price
    assert episode["anchor_reversal_direction"] == detection.reversal_direction
    assert episode["market_truth_complete"] is False
    assert episode["abnormal_score_passed"] is False
    assert "OPEN_INTEREST_DISLOCATION_UNAVAILABLE" in episode["rejection_reasons"]
    assert result["control_qualification"]["matched_pairs"] == 0
    assert result["control_qualification"]["all_matched_pairs"] == 1
    assert result["diagnosis"]["exit_testing_authorized"] is False
