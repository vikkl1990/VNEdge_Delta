from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vnedge.research.liquidity_pool_squeeze_study import (
    STUDY_ID,
    SweepEvent,
    build_feature_frame,
    detect_sweep_events,
    load_config,
    matched_control_report,
    simulate_variant,
    variant_events,
)
from vnedge.scalping.delta_engine.types import Candle, Side

CONFIG = Path("configs/research/liquidity_pool_squeeze_v1.yaml")


def _candles(count: int = 280) -> list[Candle]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        center = 100.0 + (index % 12) * 0.01
        rows.append(
            Candle(
                ts=start + timedelta(minutes=15 * (index + 1)),
                open=center,
                high=center + 0.20,
                low=center - 0.20,
                close=center + (0.01 if index % 2 else -0.01),
                volume=100.0 + index % 5,
                tf="15m",
            )
        )
    return rows


def _event(index: int, **changes) -> SweepEvent:
    values = {
        "event_id": f"BTCUSD:{index}:long",
        "symbol": "BTCUSD",
        "decision_index": index,
        "decision_ts": datetime(2025, 1, 1, tzinfo=UTC) + timedelta(minutes=15 * index),
        "side": Side.LONG,
        "pool_id": "pool",
        "pool_side": "low",
        "pool_price": 99.0,
        "pool_touches": 2,
        "pool_age_bars": 5,
        "sweep_extreme": 98.9,
        "wick_ratio": 0.6,
        "relative_volume": 1.5,
        "squeeze_recent": True,
        "bb_width_percentile": 0.1,
        "atr_bps": 20.0,
        "volatility_bucket": "medium",
        "session": "asia",
        "opposing_pool_price": 101.0,
        "target_room_bps": 200.0,
        "swept_pool_count": 1,
    }
    values.update(changes)
    return SweepEvent(**values)


def test_contract_is_research_only_and_tail_is_sealed() -> None:
    config = load_config(CONFIG)
    assert config["contract_id"] == STUDY_ID
    assert config["can_trade"] is False
    assert config["can_promote"] is False
    assert config["data"]["selection_end_exclusive"] == config["data"]["sealed_tail_start"]
    assert config["policy"]["amf_v3_evidence_mixing"] == "forbidden"


def test_variant_order_and_ablation_semantics_are_frozen() -> None:
    config = load_config(CONFIG)
    events = [
        _event(220),
        _event(221, squeeze_recent=False),
        _event(222, relative_volume=1.0),
        _event(223, target_room_bps=40.0),
    ]
    variants = variant_events(events, config)
    assert list(variants) == [
        "pool_sweep_standalone",
        "pool_squeeze_full",
        "ablate_squeeze",
        "ablate_volume",
        "ablate_target_room",
    ]
    assert len(variants["pool_sweep_standalone"]) == 4
    assert len(variants["pool_squeeze_full"]) == 1
    assert {event.decision_index for event in variants["ablate_squeeze"]} == {220, 221}
    assert {event.decision_index for event in variants["ablate_volume"]} == {220, 222}
    assert {event.decision_index for event in variants["ablate_target_room"]} == {220, 223}


def test_future_mutation_cannot_change_earlier_sweep_events() -> None:
    config = load_config(CONFIG)
    candles = _candles()
    frame = build_feature_frame(candles, config)
    cutoff = 250
    before = detect_sweep_events(frame.iloc[:cutoff].copy(), symbol="BTCUSD", config=config)
    changed = frame.copy()
    changed.loc[cutoff:, ["open", "high", "low", "close"]] *= 3.0
    after = detect_sweep_events(changed, symbol="BTCUSD", config=config)
    earlier_after = [event for event in after if event.decision_index < cutoff]
    assert before == earlier_after


def test_control_outcome_is_known_before_event() -> None:
    config = load_config(CONFIG)
    frame = build_feature_frame(_candles(360), config)
    events = [_event(260), _event(300)]
    excluded = {event.decision_index for event in events} | {255, 295}
    report = matched_control_report(frame, events, config=config, excluded_event_indices=excluded)
    assert report["matched_pairs"] == 2
    assert report["all_controls_precede_events"] is True
    assert all(pair["control_index"] + 4 < pair["event_index"] for pair in report["pairs"])
    assert all(pair["control_index"] not in excluded for pair in report["pairs"])


def test_simulator_accepts_valid_long_stop_geometry() -> None:
    config = load_config(CONFIG)
    frame = build_feature_frame(_candles(300), config)
    event = _event(
        240,
        sweep_extreme=float(frame.iloc[240]["low"]),
        opposing_pool_price=None,
        target_room_bps=None,
    )
    trades, rejected = simulate_variant(
        frame, [event], variant="pool_sweep_standalone", config=config
    )
    assert len(trades) == 1
    assert trades[0].stop_bps > 0
    assert trades[0].capture_ratio >= 0
    assert rejected["invalid_geometry"] == 0


def test_invalid_tail_overlap_fails_closed(tmp_path: Path) -> None:
    config = deepcopy(load_config(CONFIG))
    config["data"]["selection_end_exclusive"] = "2026-04-03T00:00:00+00:00"
    path = tmp_path / "bad.yaml"
    path.write_text(__import__("yaml").safe_dump(config))
    try:
        load_config(path)
    except ValueError as exc:
        assert "sealed tail" in str(exc)
    else:
        raise AssertionError("overlapping selection must fail closed")
