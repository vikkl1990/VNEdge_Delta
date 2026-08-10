"""Frozen scalper parameter registry."""

import pytest

from vnedge.scalping.parameter_registry import (
    DEFAULT_SCALPER_PARAMETER_REGISTRY as REGISTRY,
    DEFAULT_ACTIVE_SCALPER_FAMILY_ID,
    REFERENCE_REPLAY_FAMILY_ID,
    ExitPolicy,
)


def test_registry_contains_scalper_timeframes_and_families():
    assert REGISTRY.context_timeframes == ("4h", "1h", "15m", "1m")
    assert "1m" not in REGISTRY.timeframes
    assert "250ms" in REGISTRY.timeframes
    assert "60s" in REGISTRY.timeframes
    assert "1m_research_proxy" in REGISTRY.timeframes
    payload = REGISTRY.to_dict()
    assert payload["timeframe_layers"]["context"] == ["4h", "1h", "15m", "1m"]
    assert "1m_research_proxy" not in payload["timeframe_layers"]["execution"]
    for family in (
        "book_imbalance_continuation",
        "forced_flow_continuation",
        "absorption_reversal",
        "microprice_dislocation",
        "liquidity_vacuum_continuation",
        "volatility_impulse",
    ):
        assert family in REGISTRY.families
        assert REGISTRY.family(family).horizons_ms
        assert REGISTRY.family_exit_policy(family).ttl_ms > 0
    assert REGISTRY.family("book_imbalance_continuation").status == "tombstoned"
    assert (
        "all 120 configs"
        in REGISTRY.family("book_imbalance_continuation").evidence
    )
    active_ids = {family.family_id for family in REGISTRY.active_research_families()}
    assert "book_imbalance_continuation" not in active_ids
    assert "forced_flow_continuation" in active_ids
    assert REGISTRY.tombstoned_families()[0].family_id == "book_imbalance_continuation"
    assert payload["families"]["book_imbalance_continuation"]["status"] == "tombstoned"
    assert payload["family_lifecycle"]["active_research"] == [
        "forced_flow_continuation",
        "absorption_reversal",
        "microprice_dislocation",
        "liquidity_vacuum_continuation",
        "volatility_impulse",
    ]
    assert payload["family_lifecycle"]["tombstoned"][0]["family_id"] == (
        "book_imbalance_continuation"
    )


def test_exchange_fee_profiles_expose_cost_hurdles():
    binance = REGISTRY.fee_profile("binanceusdm")
    bybit = REGISTRY.fee_profile("bybit")

    assert binance.maker_first_cost_bps == pytest.approx(9.0)
    assert binance.taker_round_trip_cost_bps == pytest.approx(12.0)
    assert bybit.taker_round_trip_cost_bps > binance.taker_round_trip_cost_bps


def test_replay_and_alpha_kwargs_are_registry_backed():
    replay = REGISTRY.replay_sweep_kwargs()
    reference = REGISTRY.replay_sweep_kwargs(family_id=REFERENCE_REPLAY_FAMILY_ID)
    alpha = REGISTRY.alpha_factory_kwargs()

    assert replay["min_imbalances"] == REGISTRY.family(
        DEFAULT_ACTIVE_SCALPER_FAMILY_ID
    ).imbalance_grid
    assert replay["family_id"] == DEFAULT_ACTIVE_SCALPER_FAMILY_ID
    assert replay["exit_policy_id"] == "adverse_cut"
    assert replay["ttl_ms"] == REGISTRY.exit_policy("adverse_cut").ttl_ms
    assert reference["family_id"] == REFERENCE_REPLAY_FAMILY_ID
    assert reference["exit_policy_id"] == "static_fast"
    assert reference["ttl_ms"] == REGISTRY.exit_policy("static_fast").ttl_ms
    assert alpha["context_timeframes"] == ("4h", "1h", "15m", "1m")
    assert 250 in alpha["horizons_ms"]
    assert 60_000 in alpha["horizons_ms"]


def test_exit_intelligence_is_explicit_about_current_live_readiness():
    static = REGISTRY.exit_policy("static_fast")
    adaptive = REGISTRY.exit_policy("adaptive_trail")
    summary = REGISTRY.exit_intelligence_summary()

    assert static.intelligence_label == "DEVELOPING"
    assert adaptive.intelligence_label == "SMART_REPLAY_READY"
    assert summary["current_live_label"] == "DEVELOPING"
    assert "static_fast" in summary["live_wired_policy_ids"]
    assert "Replay can evaluate adaptive exits" in summary["assessment"]


def test_invalid_exit_policy_rejects_missing_smart_thresholds():
    with pytest.raises(ValueError, match="adverse_cut_bps"):
        ExitPolicy(
            "bad", "adverse_cut", ttl_ms=1_000,
            stop_bps=6.0, target_bps=8.0, max_hold_ms=5_000,
        )
