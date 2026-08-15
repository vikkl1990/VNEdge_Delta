"""Causality, economics, controls, and safety for liquidity survival v1."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vnedge.research.event_response_atlas import PriceTape
from vnedge.research.liquidity_survival_sweep_control import (
    ConfirmedLevel,
    LiquidityLifecycle,
    OnlineSurvivalCalibrator,
    QualityEpisodeRecord,
    ScaleSpec,
    confirmed_levels,
    load_config,
    prior_matched_controls,
    qualify_episode,
)
from vnedge.scalping.delta_engine.types import Candle

CONFIG = Path("configs/research/liquidity_survival_sweep_control_v1.yaml")


def _episode(*, direction_state: str | None = "reversal_reclaim") -> QualityEpisodeRecord:
    decision = datetime(2026, 8, 10, 4, tzinfo=UTC)
    return QualityEpisodeRecord(
        key="event-1",
        symbol="ETHUSD",
        decision_ts=decision,
        quality_available_ts=decision + timedelta(seconds=3),
        anchor_price=100.03,
        reversal_direction=-1,
        market_truth_complete=True,
        abnormal_score=0.90,
        abnormal_score_passed=True,
        direction_state=direction_state,
        directional_mfe_bps=30.0,
    )


def _level(
    identity: str,
    *,
    side: str,
    price: float,
    confirmed_at: datetime,
    scale: str,
) -> ConfirmedLevel:
    return ConfirmedLevel(
        level_id=identity,
        symbol="ETHUSD",
        side=side,
        price=price,
        scale=scale,
        confirmed_at=confirmed_at,
        expires_at=confirmed_at + timedelta(days=5),
    )


def test_online_survival_probability_cannot_see_an_unobserved_future_resolution():
    model = OnlineSurvivalCalibrator(
        alpha=2,
        beta=2,
        minimum_resolved=2,
        age_buckets=(16, 48, 192),
        touch_buckets=(1, 2, 4),
    )

    before = model.estimate(side="high", age_bars=4, touches=0, volatility_bucket="medium")
    # A future outcome has no effect until the completed resolving bar calls observe().
    still_before = model.estimate(side="high", age_bars=4, touches=0, volatility_bucket="medium")
    model.observe(
        side="high",
        age_bars=4,
        touches=0,
        volatility_bucket="medium",
        survived=True,
    )
    after = model.estimate(side="high", age_bars=4, touches=0, volatility_bucket="medium")

    assert before == still_before
    assert before.resolved_interactions == 0
    assert after.resolved_interactions == 1
    assert after.probability > before.probability


def test_pivot_level_exists_only_after_frozen_right_confirmation_bars():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    highs = [100, 101, 105, 102, 101]
    candles = [
        Candle(
            ts=start + timedelta(minutes=15 * (index + 1)),
            open=100,
            high=high,
            low=99,
            close=100,
            volume=1,
            tf="15m",
        )
        for index, high in enumerate(highs)
    ]
    spec = ScaleSpec("fast_15m", "15m", 2, 2, 100)

    schedule = confirmed_levels(candles, symbol="ETHUSD", spec=spec)

    confirmation = candles[4].ts
    assert list(schedule) == [confirmation]
    assert schedule[confirmation][0].price == 105
    assert schedule[confirmation][0].confirmed_at > candles[2].ts


def test_multi_level_reclaim_can_pass_only_with_prior_survival_and_target_room():
    config = load_config(CONFIG)
    lifecycle = LiquidityLifecycle("ETHUSD", config)
    episode = _episode()
    confirmed = episode.decision_ts - timedelta(hours=1)
    lifecycle.add_level(
        _level("fast", side="high", price=100.00, confirmed_at=confirmed, scale="fast_15m")
    )
    lifecycle.add_level(
        _level("medium", side="high", price=100.02, confirmed_at=confirmed, scale="medium_15m")
    )
    lifecycle.add_level(
        _level("target", side="low", price=99.00, confirmed_at=confirmed, scale="slow_1h")
    )
    for _ in range(20):
        lifecycle.calibrator.observe(
            side="high",
            age_bars=4,
            touches=0,
            volatility_bucket="medium",
            survived=True,
        )
    location = lifecycle.snapshot(
        episode,
        volatility_bucket="medium",
        last_closed_ts=episode.decision_ts,
    )

    result = qualify_episode(episode, location, config=config, cost_bps=14.8)

    assert location.merged_members == 2
    assert location.survival_resolved_interactions == 20
    assert location.target_room_bps is not None and location.target_room_bps > 74.0
    assert result["selected"] is True
    assert result["prior_expected_net_bps"] > 0
    assert result["prior_expected_net_is_calibrated_realized_edge"] is False


def test_prior_control_must_finish_before_episode_and_outperform_gate_is_explicit():
    config = deepcopy(load_config(CONFIG))
    config["controls"]["prior_offsets_ms"] = [-3_600_000]
    config["controls"]["minimum_pairs"] = 1
    episode = _episode()
    event_us = int(episode.quality_available_ts.timestamp() * 1_000_000)
    control_us = event_us - 3_600_000_000
    points = [
        (control_us - 300_000_000, 99.98),
        (control_us, 100.00),
        (control_us + 250_000, 100.00),
        (control_us + 900_250_000, 100.05),
        (event_us - 300_000_000, 99.98),
        (event_us, 100.00),
        (event_us + 250_000, 100.00),
        (event_us + 900_250_000, 100.25),
    ]
    tape = PriceTape(tuple(ts for ts, _ in points), tuple(price for _, price in points))

    result = prior_matched_controls(
        [{"episode_key": episode.key}],
        [episode],
        {"ETHUSD": tape},
        config=config,
    )

    assert result["matched_pairs"] == 1
    assert result["average_uplift_bps"] > 0
    assert result["checks"]["all_controls_precede_and_resolve"] is True
    assert result["passed"] is True


def test_contract_keeps_exits_tail_registry_and_amf_fail_closed():
    config = load_config(CONFIG)

    assert config["research_only"] is True
    assert config["can_trade"] is False
    assert config["can_promote"] is False
    assert config["policy"]["exits_before_control_pass"] == "forbidden"
    assert config["policy"]["sealed_tail_auto_open"] is False
    assert config["policy"]["registry_enrollment_before_selection_pass"] == "forbidden"
    assert config["policy"]["amf_v3_evidence_mixing"] == "forbidden"
