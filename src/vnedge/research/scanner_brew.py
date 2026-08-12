"""Causal scanner-brew diagnostics over recorded Delta trade prints.

This module deliberately searches only a small, declared diagnostic matrix:
reversal versus continuation direction, five entry delays, ten fixed holds, and
a handful of already-journaled absorption cohorts. Results use one active
observation per symbol and 14.8 bps taker/taker costs. They are research leads,
not fitted scanner parameters or promotion evidence.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from vnedge.replay.models import ReplayConfig
from vnedge.replay.store import DeltaShardEventStore

ENTRY_DELAYS_MS = (100, 500, 1_000, 2_000, 5_000)
HOLD_HORIZONS_MS = (
    1_000,
    5_000,
    15_000,
    30_000,
    60_000,
    90_000,
    180_000,
    300_000,
    600_000,
    900_000,
)
ROUND_TRIP_COST_BPS = 14.8
CAPTURE_ENTRY_DELAY_MS = 5_000
CAPTURE_HORIZONS_MS = (60_000, 180_000, 300_000, 600_000, 900_000)
CONTROL_SHIFTS_MS = (-1_800_000, -900_000, 900_000, 1_800_000)


def build_scanner_brew_report(
    journal_path: Path | str,
    event_root: Path | str,
    *,
    output_path: Path | str = Path("research/live_research/delta_scanner_brew_latest.json"),
    entry_delays_ms: tuple[int, ...] = ENTRY_DELAYS_MS,
    hold_horizons_ms: tuple[int, ...] = HOLD_HORIZONS_MS,
    round_trip_cost_bps: float = ROUND_TRIP_COST_BPS,
) -> dict[str, object]:
    _validate_grid(entry_delays_ms, hold_horizons_ms, round_trip_cost_bps)
    observations = _load_observations(Path(journal_path))
    if not observations:
        raise ValueError("no completed absorption observations found")
    first_ns = min(row["decision_ns"] for row in observations)
    last_ns = max(row["decision_ns"] for row in observations)
    trade_tape = _load_trade_tape(
        Path(event_root),
        start_ns=first_ns - 2_000_000_000,
        end_ns=last_ns + (max(entry_delays_ms) + max(hold_horizons_ms) + 2_000) * 1_000_000,
    )
    variants = []
    recipes = _recipes()
    for recipe_name, direction_mode, predicate in recipes:
        for entry_delay_ms in entry_delays_ms:
            for hold_ms in hold_horizons_ms:
                rows = _simulate(
                    observations,
                    trade_tape,
                    direction_mode=direction_mode,
                    predicate=predicate,
                    entry_delay_ms=entry_delay_ms,
                    hold_ms=hold_ms,
                    round_trip_cost_bps=round_trip_cost_bps,
                )
                metrics = _metrics(rows)
                variants.append(
                    {
                        "recipe": recipe_name,
                        "direction_mode": direction_mode,
                        "entry_delay_ms": entry_delay_ms,
                        "hold_ms": hold_ms,
                        **metrics,
                    }
                )
    ranked = sorted(
        variants,
        key=lambda row: (row["average_net_bps"], row["observations"]),
        reverse=True,
    )
    flaw_decomposition = _flaw_decomposition(
        observations,
        trade_tape,
        variants,
        round_trip_cost_bps=round_trip_cost_bps,
    )
    movement_capture = _movement_capture_diagnostics(
        observations,
        trade_tape,
        entry_delay_ms=CAPTURE_ENTRY_DELAY_MS,
        hold_horizons_ms=CAPTURE_HORIZONS_MS,
        control_shifts_ms=CONTROL_SHIFTS_MS,
        round_trip_cost_bps=round_trip_cost_bps,
    )
    populated = [row for row in ranked if row["observations"] >= 100]
    positive = [
        row
        for row in populated
        if row["average_net_bps"] > 0
        and (row["profit_factor_net"] or 0.0) > 1.0
        and row["first_half_average_net_bps"] > 0
        and row["second_half_average_net_bps"] > 0
    ]
    payload: dict[str, object] = {
        "schema_version": "vnedge.scanner_brew.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "journal_path": str(journal_path),
            "event_root": str(event_root),
            "completed_observations": len(observations),
            "first_decision_ts": observations[0]["decision_ts"],
            "last_decision_ts": observations[-1]["decision_ts"],
            "trade_prints": sum(len(rows["prices"]) for rows in trade_tape.values()),
        },
        "assumptions": {
            "fill_proxy": "first_public_trade_at_or_after_entry_delay",
            "exit_proxy": "first_public_trade_at_or_after_fixed_hold",
            "fill_model_warning": "optimistic_trade_print_proxy_not_executable_bid_ask",
            "one_active_observation_per_symbol": True,
            "round_trip_cost_bps": round_trip_cost_bps,
            "entry_delays_ms": list(entry_delays_ms),
            "hold_horizons_ms": list(hold_horizons_ms),
            "parameter_matrix_is_diagnostic_not_selection": True,
        },
        "flaw_decomposition": flaw_decomposition,
        "movement_capture": movement_capture,
        "top_variants_min_100": populated[:20],
        "positive_stable_variants_min_100": positive,
        "all_variants": ranked,
        "diagnosis": {
            "best_variant_min_100": populated[0] if populated else None,
            "after_cost_edge_found": bool(positive),
            "verdict": (
                "DIAGNOSTIC_LEAD_REQUIRES_PREREGISTRATION"
                if positive
                else "NO_AFTER_COST_EDGE_IN_DECLARED_MATRIX"
            ),
            "next_hypothesis": ("failed_auction_response_after_flow_reversal_and_failed_retest"),
            "note": (
                "Raw absorption direction, entry delay, and fixed hold permutations "
                "are diagnostics. They cannot authorize scanner implementation or "
                "selection under the frozen failed-auction v1 contract."
            ),
            "movement_capture_verdict": movement_capture["verdict"],
        },
        "research_only": True,
        "scanner_implementation_authorized": False,
        "selection_authorized": False,
        "can_trade": False,
        "can_promote": False,
        "order_route": "absent",
    }
    _atomic_json(Path(output_path), payload)
    return payload


def _validate_grid(
    entry_delays_ms: tuple[int, ...],
    hold_horizons_ms: tuple[int, ...],
    round_trip_cost_bps: float,
) -> None:
    if not entry_delays_ms or any(value < 0 for value in entry_delays_ms):
        raise ValueError("entry delays must be non-negative")
    if not hold_horizons_ms or any(value <= 0 for value in hold_horizons_ms):
        raise ValueError("hold horizons must be positive")
    if round_trip_cost_bps <= 0:
        raise ValueError("round-trip cost must be positive")


def _load_observations(path: Path) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid journal JSON at line {line_number}") from exc
            if envelope.get("kind") != "delta_absorption_research_outcome":
                continue
            payload = envelope.get("payload")
            if not isinstance(payload, dict) or payload.get("entry_price") is None:
                continue
            key = str(payload.get("key") or "")
            if not key:
                raise ValueError(f"outcome missing key at line {line_number}")
            previous = unique.get(key)
            if previous is not None and previous != payload:
                raise ValueError(f"conflicting outcome key: {key}")
            unique[key] = payload
    rows = []
    for payload in unique.values():
        decision_ts = str(payload["decision_ts"])
        rows.append(
            {
                **payload,
                "decision_ns": int(datetime.fromisoformat(decision_ts).timestamp() * 1e9),
            }
        )
    return sorted(rows, key=lambda row: (row["decision_ns"], row["key"]))


def _load_trade_tape(
    event_root: Path, *, start_ns: int, end_ns: int
) -> dict[str, dict[str, list[float] | list[int]]]:
    symbols = ("BTCUSD", "ETHUSD")
    config = ReplayConfig(
        symbols=symbols,
        start_ts_us=max(0, start_ns // 1_000 - 2_000_000),
        end_ts_us=end_ns // 1_000 + 2_000_000,
        channels=("trades",),
        enable_scanner=False,
        journal_mode="none",
        code_version="scanner-brew-read-only",
    )
    result: dict[str, dict[str, list[float] | list[int]]] = {
        symbol: {"times": [], "prices": []} for symbol in symbols
    }
    seen: set[tuple[str, int, str]] = set()
    for event in DeltaShardEventStore(event_root).iter_events(config):
        if not start_ns <= event.local_recv_ns < end_ns:
            continue
        price_raw = event.raw_message.get("p")
        if price_raw is None:
            continue
        identity = (event.symbol, event.exchange_timestamp_us, str(event.raw_message))
        if identity in seen:
            continue
        seen.add(identity)
        result[event.symbol]["times"].append(event.local_recv_ns)
        result[event.symbol]["prices"].append(float(price_raw))
    for symbol, rows in result.items():
        if not rows["times"]:
            raise ValueError(f"recorded trade tape is empty for {symbol}")
    return result


def _recipes() -> tuple[tuple[str, str, Callable[[dict[str, Any]], bool]], ...]:
    return (
        ("raw_absorption_reversal", "reversal", lambda row: True),
        ("raw_absorption_continuation", "continuation", lambda row: True),
        ("single_level_reversal", "reversal", lambda row: not row["was_stacked"]),
        ("stacked_reversal", "reversal", lambda row: bool(row["was_stacked"])),
        (
            "high_strength_reversal",
            "reversal",
            lambda row: float(row["event"]["strength"]) >= 0.90,
        ),
        (
            "high_volume_reversal",
            "reversal",
            lambda row: float(row["volume_percentile"]) >= 0.80,
        ),
        (
            "long_reversal",
            "reversal",
            lambda row: int(row["event"]["reversal_direction"]) > 0,
        ),
        (
            "short_reversal",
            "reversal",
            lambda row: int(row["event"]["reversal_direction"]) < 0,
        ),
    )


def _simulate(
    observations: list[dict[str, Any]],
    trade_tape: dict[str, dict[str, list[float] | list[int]]],
    *,
    direction_mode: str,
    predicate: Callable[[dict[str, Any]], bool],
    entry_delay_ms: int,
    hold_ms: int,
    round_trip_cost_bps: float,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    next_available_ns = {"BTCUSD": 0, "ETHUSD": 0}
    for row in observations:
        if not predicate(row):
            continue
        symbol = str(row["symbol"])
        decision_ns = int(row["decision_ns"])
        if decision_ns < next_available_ns[symbol]:
            continue
        times = trade_tape[symbol]["times"]
        prices = trade_tape[symbol]["prices"]
        assert isinstance(times, list) and isinstance(prices, list)
        entry_index = bisect.bisect_left(times, decision_ns + entry_delay_ms * 1_000_000)
        if entry_index >= len(times):
            continue
        entry_ns = int(times[entry_index])
        exit_index = bisect.bisect_left(times, entry_ns + hold_ms * 1_000_000)
        if exit_index >= len(times):
            continue
        exit_ns = int(times[exit_index])
        direction = int(row["event"]["reversal_direction"])
        if direction_mode == "continuation":
            direction *= -1
        entry_price = float(prices[entry_index])
        exit_price = float(prices[exit_index])
        gross_bps = direction * (exit_price / entry_price - 1.0) * 10_000.0
        results.append(
            {
                "symbol": symbol,
                "decision_ns": decision_ns,
                "entry_ns": entry_ns,
                "exit_ns": exit_ns,
                "gross_bps": gross_bps,
                "net_bps": gross_bps - round_trip_cost_bps,
            }
        )
        next_available_ns[symbol] = exit_ns
    return results


def _profit_factor(values: Iterable[float]) -> float | None:
    rows = list(values)
    gains = sum(value for value in rows if value > 0)
    losses = abs(sum(value for value in rows if value < 0))
    if losses:
        return gains / losses
    return None if gains else 0.0


def _metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    if not rows:
        return {
            "observations": 0,
            "average_gross_bps": 0.0,
            "average_net_bps": 0.0,
            "profit_factor_gross": 0.0,
            "profit_factor_net": 0.0,
            "gross_positive_rate": 0.0,
            "net_positive_rate": 0.0,
            "first_half_average_net_bps": 0.0,
            "second_half_average_net_bps": 0.0,
            "btc_average_net_bps": 0.0,
            "eth_average_net_bps": 0.0,
        }
    ordered = sorted(rows, key=lambda row: int(row["decision_ns"]))
    split = len(ordered) // 2
    gross = [float(row["gross_bps"]) for row in ordered]
    net = [float(row["net_bps"]) for row in ordered]

    def average(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def symbol_average(symbol: str) -> float:
        return average([float(row["net_bps"]) for row in ordered if row["symbol"] == symbol])

    return {
        "observations": len(ordered),
        "average_gross_bps": average(gross),
        "average_net_bps": average(net),
        "profit_factor_gross": _profit_factor(gross),
        "profit_factor_net": _profit_factor(net),
        "gross_positive_rate": sum(value > 0 for value in gross) / len(gross),
        "net_positive_rate": sum(value > 0 for value in net) / len(net),
        "first_half_average_net_bps": average(net[:split]),
        "second_half_average_net_bps": average(net[split:]),
        "btc_average_net_bps": symbol_average("BTCUSD"),
        "eth_average_net_bps": symbol_average("ETHUSD"),
    }


def _flaw_decomposition(
    observations: list[dict[str, Any]],
    trade_tape: dict[str, dict[str, list[float] | list[int]]],
    variants: list[dict[str, object]],
    *,
    round_trip_cost_bps: float,
) -> dict[str, object]:
    completed = len(observations)
    stop_then_target = sum(
        bool(row["stopped_out"]) and bool(row["hit_target_1"]) for row in observations
    )
    target_ticks = [
        float(row["realized_gross_ticks"]) for row in observations if row["hit_target_1"]
    ]
    target_bps = []
    for row in observations:
        if not row["hit_target_1"]:
            continue
        tick_size = 0.5 if row["symbol"] == "BTCUSD" else 0.05
        target_bps.append(8.0 * tick_size / float(row["entry_price"]) * 10_000.0)
    episode_count = 0
    next_episode_ns = {"BTCUSD": 0, "ETHUSD": 0}
    for row in observations:
        symbol = str(row["symbol"])
        if int(row["decision_ns"]) >= next_episode_ns[symbol]:
            episode_count += 1
            next_episode_ns[symbol] = int(row["decision_ns"]) + 20_000_000_000
    raw_variants = [
        row
        for row in variants
        if row["recipe"] in {"raw_absorption_reversal", "raw_absorption_continuation"}
    ]
    best_raw = max(raw_variants, key=lambda row: row["average_gross_bps"])
    reversal_variants = [row for row in variants if row["recipe"] == "raw_absorption_reversal"]
    preferred_horizon = (
        60_000
        if any(row["hold_ms"] == 60_000 for row in reversal_variants)
        else max(int(row["hold_ms"]) for row in reversal_variants)
    )
    best_delay = max(
        (row for row in reversal_variants if row["hold_ms"] == preferred_horizon),
        key=lambda row: row["average_gross_bps"],
    )
    return {
        "entry": {
            "current_model": "next_public_trade_after_detection",
            "flaw": "not_executable_bid_ask_and_has_no_signal_to_fill_latency",
            "tested_delays_ms": sorted({int(row["entry_delay_ms"]) for row in variants}),
            "best_reversal_delay_at_reference_hold": best_delay,
        },
        "hold": {
            "best_raw_direction_delay_hold": best_raw,
            "gross_edge_gap_to_cost_bps": (
                float(best_raw["average_gross_bps"]) - round_trip_cost_bps
            ),
            "flaw": "raw_absorption_response_is_near_zero_at_every_tested_horizon",
        },
        "exit": {
            "average_fixed_target_bps": (sum(target_bps) / len(target_bps) if target_bps else 0.0),
            "round_trip_cost_bps": round_trip_cost_bps,
            "target_minus_cost_bps": (
                sum(target_bps) / len(target_bps) - round_trip_cost_bps
                if target_bps
                else -round_trip_cost_bps
            ),
            "target_hits_recorded": len(target_ticks),
            "stopped_then_later_hit_target": stop_then_target,
            "stopped_then_later_hit_target_rate": (
                stop_then_target / completed if completed else 0.0
            ),
            "flaw": "target_is_smaller_than_cost_and_post_stop_hits_pollute_hit_rate",
        },
        "sampling": {
            "raw_observations": completed,
            "non_overlapping_20s_episodes": episode_count,
            "overlap_multiplier": completed / episode_count if episode_count else 0.0,
            "flaw": "many_detector_observations_are_correlated_repeats_of_one_flow_episode",
        },
        "data": {
            "trade_print_counts": {
                symbol: len(rows["prices"]) for symbol, rows in trade_tape.items()
            },
            "warning": "trade_print_fill_proxy_is_optimistic; quote_replay_will_be_worse",
        },
    }


def _movement_capture_diagnostics(
    observations: list[dict[str, Any]],
    trade_tape: dict[str, dict[str, list[float] | list[int]]],
    *,
    entry_delay_ms: int,
    hold_horizons_ms: tuple[int, ...],
    control_shifts_ms: tuple[int, ...],
    round_trip_cost_bps: float,
) -> dict[str, object]:
    """Separate movement selection, side selection, and exit capture.

    The event detector must first identify more future path movement than nearby
    non-event windows. Only then does direction and exit logic matter. Controls
    are deterministic time-shifts of the same non-overlapping event episodes;
    they are a diagnostic baseline, not a matched causal estimate.
    """

    horizons: list[dict[str, object]] = []
    for hold_ms in hold_horizons_ms:
        event_rows: list[dict[str, float]] = []
        control_rows: dict[int, list[dict[str, float]]] = {
            shift: [] for shift in control_shifts_ms
        }
        next_available_ns = {symbol: 0 for symbol in trade_tape}
        for row in observations:
            symbol = str(row["symbol"])
            decision_ns = int(row["decision_ns"])
            if decision_ns < next_available_ns.get(symbol, 0):
                continue
            direction = int(row["event"]["reversal_direction"])
            event = _path_capture_row(
                trade_tape[symbol],
                start_ns=decision_ns + entry_delay_ms * 1_000_000,
                hold_ms=hold_ms,
                direction=direction,
            )
            if event is None:
                continue
            event_rows.append(event)
            next_available_ns[symbol] = int(event["exit_ns"])
            for shift_ms in control_shifts_ms:
                control = _path_capture_row(
                    trade_tape[symbol],
                    start_ns=(
                        decision_ns + (entry_delay_ms + shift_ms) * 1_000_000
                    ),
                    hold_ms=hold_ms,
                    direction=direction,
                )
                if control is not None:
                    control_rows[shift_ms].append(control)

        summary = _capture_summary(event_rows, round_trip_cost_bps)
        controls = []
        for shift_ms, rows in control_rows.items():
            control = _capture_summary(rows, round_trip_cost_bps)
            controls.append({"shift_ms": shift_ms, **control})
        populated_control_oracles = [
            float(row["average_oracle_path_bps"])
            for row in controls
            if int(row["observations"]) > 0
        ]
        control_reference = _median(populated_control_oracles)
        event_oracle = float(summary["average_oracle_path_bps"])
        movement_lift = event_oracle - control_reference if control_reference else 0.0
        horizons.append(
            {
                "hold_ms": hold_ms,
                **summary,
                "control_reference_oracle_bps": control_reference,
                "movement_lift_vs_control_bps": movement_lift,
                "movement_lift_ratio": (
                    event_oracle / control_reference if control_reference else 0.0
                ),
                "nearby_controls": controls,
            }
        )

    longest = horizons[-1] if horizons else {}
    event_oracle = float(longest.get("average_oracle_path_bps") or 0.0)
    selected_mfe = float(longest.get("average_selected_mfe_bps") or 0.0)
    realized = float(longest.get("average_fixed_exit_gross_bps") or 0.0)
    control_lift = float(longest.get("movement_lift_vs_control_bps") or 0.0)
    return {
        "entry_delay_ms": entry_delay_ms,
        "control_design": "same_event_episodes_shifted_by_plus_or_minus_15_and_30_minutes",
        "control_warning": (
            "diagnostic_nearby_windows_are_correlated_and_do_not_authorize_selection"
        ),
        "horizons": horizons,
        "longest_horizon_decomposition": {
            "market_opportunity_bps": event_oracle,
            "movement_lift_vs_nearby_control_bps": control_lift,
            "selected_direction_mfe_bps": selected_mfe,
            "fixed_exit_realized_gross_bps": realized,
            "round_trip_cost_bps": round_trip_cost_bps,
            "opportunity_to_direction_loss_bps": event_oracle - selected_mfe,
            "direction_to_exit_giveback_bps": selected_mfe - realized,
            "realized_gap_to_cost_bps": realized - round_trip_cost_bps,
        },
        "verdict": "NO_PROVEN_MOVEMENT_SELECTION_OR_AFTER_COST_CAPTURE",
        "interpretation": (
            "Nearby windows show similar future path movement. Raw absorption does "
            "not isolate exceptional expansion; side selection and fixed exits lose "
            "additional bps before the fee wall."
        ),
    }


def _path_capture_row(
    tape: dict[str, list[float] | list[int]],
    *,
    start_ns: int,
    hold_ms: int,
    direction: int,
) -> dict[str, float] | None:
    times = tape["times"]
    prices = tape["prices"]
    assert isinstance(times, list) and isinstance(prices, list)
    entry_index = bisect.bisect_left(times, start_ns)
    if entry_index >= len(times):
        return None
    entry_ns = int(times[entry_index])
    exit_index = bisect.bisect_left(times, entry_ns + hold_ms * 1_000_000)
    if exit_index >= len(times):
        return None
    entry_price = float(prices[entry_index])
    path = [float(value) for value in prices[entry_index : exit_index + 1]]
    if entry_price <= 0 or not path:
        return None
    long_mfe = (max(path) / entry_price - 1.0) * 10_000.0
    short_mfe = (entry_price / min(path) - 1.0) * 10_000.0
    selected_mfe = long_mfe if direction > 0 else short_mfe
    selected_mae = short_mfe if direction > 0 else long_mfe
    fixed_exit = direction * (path[-1] / entry_price - 1.0) * 10_000.0
    return {
        "exit_ns": float(times[exit_index]),
        "oracle_path_bps": max(long_mfe, short_mfe),
        "selected_mfe_bps": selected_mfe,
        "selected_mae_bps": selected_mae,
        "fixed_exit_gross_bps": fixed_exit,
        "absolute_fixed_exit_bps": abs((path[-1] / entry_price - 1.0) * 10_000.0),
    }


def _capture_summary(
    rows: list[dict[str, float]], round_trip_cost_bps: float
) -> dict[str, object]:
    def average(key: str) -> float:
        return sum(row[key] for row in rows) / len(rows) if rows else 0.0

    oracle = average("oracle_path_bps")
    selected_mfe = average("selected_mfe_bps")
    fixed_exit = average("fixed_exit_gross_bps")
    return {
        "observations": len(rows),
        "average_oracle_path_bps": oracle,
        "average_selected_mfe_bps": selected_mfe,
        "average_selected_mae_bps": average("selected_mae_bps"),
        "average_absolute_fixed_exit_bps": average("absolute_fixed_exit_bps"),
        "average_fixed_exit_gross_bps": fixed_exit,
        "direction_accuracy": (
            sum(row["fixed_exit_gross_bps"] > 0 for row in rows) / len(rows)
            if rows
            else 0.0
        ),
        "oracle_path_cost_clear_rate": (
            sum(row["oracle_path_bps"] >= round_trip_cost_bps for row in rows) / len(rows)
            if rows
            else 0.0
        ),
        "selected_mfe_cost_clear_rate": (
            sum(row["selected_mfe_bps"] >= round_trip_cost_bps for row in rows) / len(rows)
            if rows
            else 0.0
        ),
        "directional_opportunity_capture_ratio": (
            selected_mfe / oracle if oracle else 0.0
        ),
        "fixed_exit_capture_ratio": (fixed_exit / selected_mfe if selected_mfe else 0.0),
    }


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=Path("logs/delta_event_research.jsonl"))
    parser.add_argument("--event-root", type=Path, default=Path("data/delta_events"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/live_research/delta_scanner_brew_latest.json"),
    )
    args = parser.parse_args(argv)
    payload = build_scanner_brew_report(
        args.journal,
        args.event_root,
        output_path=args.output,
    )
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
