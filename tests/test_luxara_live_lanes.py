"""Luxara scanners are enrolled in the live Delta shadow roster.

Guards the wiring added when the two Pine-derived Luxara scanners were promoted
from research-only into the live multi-lane shadow roster: the lane builders
must emit shadow-first 5m Delta lanes, ``desired_lane_specs`` must include them,
and the lane factory must be able to construct BOTH strategies (break-bounce was
previously unwired and would have raised ``unsupported lane strategy_id``).
"""

from vnedge.runtime.multi_lane import _build_single_strategy
from vnedge.runtime.multi_lane_shadow import (
    desired_lane_specs,
    luxara_break_bounce_v27_delta_lanes,
    luxara_live_plan_qtm_delta_lanes,
)
from vnedge.runtime.runner_config import RunnerMode
from vnedge.strategy.luxara_break_bounce_v27 import (
    LUXARA_BREAK_BOUNCE_V27_ID,
    LuxaraBreakBounceV27Scanner,
)
from vnedge.strategy.luxara_live_plan_qtm import (
    LUXARA_LIVE_PLAN_QTM_ID,
    LuxaraLivePlanQTMScanner,
)

_DELTA_SYMBOLS = {"ETH/USD:USD", "BTC/USD:USD", "SOL/USD:USD", "XRP/USD:USD"}


def test_luxara_live_plan_qtm_delta_lanes_are_shadow_first_and_5m():
    lanes = luxara_live_plan_qtm_delta_lanes({})

    assert {lane.symbol for lane in lanes} == _DELTA_SYMBOLS
    assert all(lane.exchange == "delta_india" for lane in lanes)
    assert all(lane.timeframe == "5m" for lane in lanes)
    assert all(lane.strategy_id == LUXARA_LIVE_PLAN_QTM_ID for lane in lanes)
    assert all(lane.mode is RunnerMode.SHADOW for lane in lanes)


def test_luxara_break_bounce_delta_lanes_are_shadow_first_and_5m():
    lanes = luxara_break_bounce_v27_delta_lanes({})

    assert {lane.symbol for lane in lanes} == _DELTA_SYMBOLS
    assert all(lane.exchange == "delta_india" for lane in lanes)
    assert all(lane.timeframe == "5m" for lane in lanes)
    assert all(lane.strategy_id == LUXARA_BREAK_BOUNCE_V27_ID for lane in lanes)
    assert all(lane.mode is RunnerMode.SHADOW for lane in lanes)
    # Edge floor aligned to the other Delta scalper lanes so it actually fires.
    assert all(
        lane.strategy_params == {"min_expected_net_edge_bps": 25.0} for lane in lanes
    )


def test_both_luxara_scanners_are_in_desired_lane_specs():
    # Both luxara scanners are pruned from the default roster by the 2026-08-03
    # hard-cut; prune-off verifies the generators still wire them into the base.
    specs = desired_lane_specs({"MULTI_LANE_PRUNE_DEAD": "0"})
    strategy_ids = {spec.strategy_id for spec in specs}

    assert LUXARA_LIVE_PLAN_QTM_ID in strategy_ids
    assert LUXARA_BREAK_BOUNCE_V27_ID in strategy_ids


def test_lane_factory_builds_both_luxara_scanners():
    # break-bounce was previously unwired; a lane for it would raise here.
    live_plan = _build_single_strategy(LUXARA_LIVE_PLAN_QTM_ID, {}, None, None)
    break_bounce = _build_single_strategy(
        LUXARA_BREAK_BOUNCE_V27_ID, {"min_expected_net_edge_bps": 25.0}, None, None
    )

    assert isinstance(live_plan, LuxaraLivePlanQTMScanner)
    assert isinstance(break_bounce, LuxaraBreakBounceV27Scanner)
    # loosened gate carried through construction
    assert live_plan.params.min_expected_net_edge_bps == 30.0
    assert break_bounce.params.min_expected_net_edge_bps == 25.0
