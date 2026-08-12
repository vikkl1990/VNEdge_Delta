"""Causality, explainability, economics, and lock tests for family scoring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vnedge.scalping.delta_engine.indicator_scoring import (
    CandidateEconomics,
    IndicatorEvidence,
    IndicatorFamily,
    IndicatorFamilyScorer,
    candle_indicator_evidence,
    default_indicator_scoring_config,
    load_indicator_scoring_config,
)
from vnedge.scalping.delta_engine.types import Side

NOW = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)


def strong_evidence() -> tuple[IndicatorEvidence, ...]:
    return (
        IndicatorEvidence("structure", IndicatorFamily.STRUCTURE, 90, 0.9, NOW, "aligned"),
        IndicatorEvidence("momentum", IndicatorFamily.MOMENTUM, 85, 0.8, NOW, "persistent"),
        IndicatorEvidence("order_flow", IndicatorFamily.ORDER_FLOW, 88, 0.9, NOW, "aligned"),
        IndicatorEvidence("liquidity", IndicatorFamily.LIQUIDITY, 90, 0.9, NOW, "liquid"),
        IndicatorEvidence("target", IndicatorFamily.ECONOMICS, 90, 1.0, NOW, "cost multiple"),
        IndicatorEvidence("net", IndicatorFamily.ECONOMICS, 85, 1.0, NOW, "net edge"),
        IndicatorEvidence("rr", IndicatorFamily.ECONOMICS, 80, 1.0, NOW, "reward risk"),
        IndicatorEvidence("integrity", IndicatorFamily.DATA_QUALITY, 100, 1.0, NOW, "valid"),
        IndicatorEvidence("fresh", IndicatorFamily.DATA_QUALITY, 95, 1.0, NOW, "fresh"),
    )


def test_loaded_policy_matches_frozen_defaults() -> None:
    path = Path("configs/research/indicator_family_scoring_v1.yaml")
    loaded = load_indicator_scoring_config(path)
    defaults = default_indicator_scoring_config()
    assert loaded == defaults
    assert loaded.research_only and not loaded.can_trade and not loaded.can_promote


def test_strong_score_is_deterministic_explainable_and_never_tradable() -> None:
    scorer = IndicatorFamilyScorer()
    first = scorer.score(symbol="ethusd", side=Side.LONG, decision_ts=NOW, evidence=strong_evidence())
    second = scorer.score(symbol="ethusd", side=Side.LONG, decision_ts=NOW, evidence=strong_evidence())
    assert first == second
    assert first.research_qualified
    assert first.composite_score >= 72
    assert first.coverage >= 0.55
    payload = first.to_dict()
    assert payload["symbol"] == "ETHUSD"
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert payload["used_for_signal"] is False
    assert payload["used_for_execution"] is False
    assert all(row["evidence"] for row in payload["families"])


def test_future_evidence_fails_closed() -> None:
    evidence = list(strong_evidence())
    evidence[0] = IndicatorEvidence(
        "structure", IndicatorFamily.STRUCTURE, 90, 1.0, NOW + timedelta(seconds=1), "future"
    )
    with pytest.raises(ValueError, match="future information"):
        IndicatorFamilyScorer().score(
            symbol="ETHUSD", side=Side.LONG, decision_ts=NOW, evidence=evidence
        )


def test_duplicate_indicator_ids_fail_closed() -> None:
    row = IndicatorEvidence("same", IndicatorFamily.STRUCTURE, 70, 1.0, NOW, "one")
    with pytest.raises(ValueError, match="unique"):
        IndicatorFamilyScorer().score(
            symbol="ETHUSD", side=Side.LONG, decision_ts=NOW, evidence=(row, row)
        )


def test_missing_economics_and_data_quality_are_explicit_blockers() -> None:
    result = IndicatorFamilyScorer().score(
        symbol="ETHUSD",
        side=Side.LONG,
        decision_ts=NOW,
        evidence=(
            IndicatorEvidence("structure", IndicatorFamily.STRUCTURE, 95, 1.0, NOW, "aligned"),
        ),
    )
    assert not result.research_qualified
    assert "missing_required_family:economics" in result.blockers
    assert "missing_required_family:data_quality" in result.blockers


def test_watch_band_is_distinct_from_a_data_or_policy_block() -> None:
    evidence = tuple(
        IndicatorEvidence(
            row.indicator_id,
            row.family,
            65.0 if row.family is not IndicatorFamily.DATA_QUALITY else row.score,
            row.confidence,
            row.available_at,
            row.reason,
            row.raw_value,
            row.weight,
        )
        for row in strong_evidence()
    )
    result = IndicatorFamilyScorer().score(
        symbol="ETHUSD", side=Side.LONG, decision_ts=NOW, evidence=evidence
    )
    assert not result.research_qualified
    assert result.quality_band == "watch"
    assert any(reason.startswith("composite_below_floor:") for reason in result.blockers)


def test_hard_data_block_overrides_a_high_composite() -> None:
    evidence = (*strong_evidence(),)
    evidence = tuple(
        IndicatorEvidence(
            row.indicator_id,
            row.family,
            row.score,
            row.confidence,
            row.available_at,
            row.reason,
            row.raw_value,
            row.weight,
            hard_block=row.indicator_id == "integrity",
        )
        for row in evidence
    )
    result = IndicatorFamilyScorer().score(
        symbol="ETHUSD", side=Side.LONG, decision_ts=NOW, evidence=evidence
    )
    assert result.composite_score >= 72
    assert not result.research_qualified
    assert result.quality_band == "blocked"
    assert "hard_block:integrity" in result.blockers


def test_candle_adapter_exposes_all_available_families_and_cost_wall() -> None:
    row = {
        "bias_long": True,
        "bias_short": False,
        "bos_up": True,
        "long_score": 8.0,
        "short_score": 2.0,
        "displacement_up": True,
        "atr_pct": 0.55,
        "volume_z": 1.4,
    }
    evidence = candle_indicator_evidence(
        row,
        side=Side.LONG,
        available_at=NOW,
        economics=CandidateEconomics(70.0, 14.0, 35.0, 3.0),
        closed_candle=True,
    )
    families = {item.family for item in evidence}
    assert {
        IndicatorFamily.STRUCTURE,
        IndicatorFamily.MOMENTUM,
        IndicatorFamily.VOLATILITY,
        IndicatorFamily.PARTICIPATION,
        IndicatorFamily.ECONOMICS,
        IndicatorFamily.DATA_QUALITY,
    }.issubset(families)
    result = IndicatorFamilyScorer().score(
        symbol="ETHUSD", side=Side.LONG, decision_ts=NOW, evidence=evidence
    )
    assert result.research_qualified


def test_open_candle_is_never_research_qualified() -> None:
    evidence = candle_indicator_evidence(
        {"bias_long": True, "bos_up": True, "long_score": 9.0, "short_score": 1.0},
        side=Side.LONG,
        available_at=NOW,
        economics=CandidateEconomics(90.0, 14.0, 40.0, 3.0),
        closed_candle=False,
    )
    result = IndicatorFamilyScorer().score(
        symbol="ETHUSD", side=Side.LONG, decision_ts=NOW, evidence=evidence
    )
    assert not result.research_qualified
    assert "hard_block:closed_candle_integrity" in result.blockers
