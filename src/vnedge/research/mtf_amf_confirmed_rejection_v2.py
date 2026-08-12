"""Selection-only revival of the MTF/AMF rejection hypothesis.

Version 1 measured a rejection candle from the next open to an arbitrary fixed
horizon.  Version 2 is a new, separately versioned hypothesis with an executable
entry and exit contract:

* the original causal 1h/4h rejection is the setup, not the entry;
* a completed 15m candle inside the following hour must confirm direction and
  fail to retest the rejection extreme;
* entry is the following 15m open;
* the stop sits beyond the rejection extreme and the target is exactly 2R;
* candidates whose target is smaller than five times modeled costs are rejected;
* stop wins any same-candle stop/target ambiguity.

This module never opens the sealed tail and has no order or paper route.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Mapping

import pandas as pd

from vnedge.research.mtf_amf_rejection_scanner import (
    MtfAmfScannerConfig,
    build_mtf_amf_feature_frame,
    fetch_delta_public_candles,
)

SCANNER_ID = "mtf_amf_confirmed_rejection_v2"
DEFAULT_OUTPUT = Path(
    "research/live_research/mtf_amf_confirmed_rejection_v2_latest.json"
)
DEFAULT_SELECTION_END = datetime(2026, 4, 1, tzinfo=UTC)
DEFAULT_UNTOUCHED_START = datetime(2026, 4, 3, tzinfo=UTC)
DEFAULT_SYMBOLS = ("BTCUSD", "ETHUSD")
Side = Literal["long", "short"]


@dataclass(frozen=True)
class ConfirmedRejectionV2Config:
    minimum_rejection_wick_ratio: float = 0.40
    confirmation_body_ratio: float = 0.50
    confirmation_window_bars: int = 4
    stop_buffer_atr: float = 0.10
    minimum_stop_bps: float = 18.0
    maximum_stop_bps: float = 150.0
    reward_risk: float = 2.0
    minimum_target_cost_multiple: float = 5.0
    maximum_hold_bars: int = 48
    cooldown_bars: int = 24
    round_trip_cost_bps: float = 14.8
    minimum_selection_trades: int = 60
    minimum_trades_per_market: int = 20
    minimum_profit_factor: float = 1.20
    minimum_average_net_bps: float = 3.0

    def __post_init__(self) -> None:
        if not 0 < self.minimum_rejection_wick_ratio <= 1:
            raise ValueError("minimum_rejection_wick_ratio must be in (0, 1]")
        if not 0 < self.confirmation_body_ratio <= 1:
            raise ValueError("confirmation_body_ratio must be in (0, 1]")
        if self.stop_buffer_atr < 0:
            raise ValueError("stop_buffer_atr cannot be negative")
        if not 0 < self.minimum_stop_bps < self.maximum_stop_bps:
            raise ValueError("stop bounds must be positive and ascending")
        if self.reward_risk <= 1 or self.minimum_target_cost_multiple <= 1:
            raise ValueError("reward/risk and target/cost multiples must exceed one")
        if self.confirmation_window_bars < 1:
            raise ValueError("confirmation_window_bars must be positive")
        if self.maximum_hold_bars < 1 or self.cooldown_bars < 0:
            raise ValueError("hold must be positive and cooldown cannot be negative")
        if self.round_trip_cost_bps <= 0:
            raise ValueError("round_trip_cost_bps must be positive")


DEFAULT_CONFIG = ConfirmedRejectionV2Config()
BASE_CONFIG = MtfAmfScannerConfig(cooldown_bars=0)


@dataclass(frozen=True)
class ConfirmedRejectionTrade:
    scanner_id: str
    symbol: str
    side: Side
    setup_ts: str
    confirmation_ts: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    stop_price: float
    target_price: float
    exit_price: float
    stop_bps: float
    target_bps: float
    cost_bps: float
    gross_bps: float
    net_bps: float
    mfe_bps: float
    mae_bps: float
    hold_bars: int
    exit_reason: str
    same_bar_ambiguous: bool
    rejection_wick_ratio: float
    confirmation_body_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _body_ratio(row: pd.Series) -> float:
    span = max(float(row["high"]) - float(row["low"]), float(row["close"]) * 1e-9)
    return abs(float(row["close"]) - float(row["open"])) / span


def _wick_ratio(row: pd.Series, side: Side) -> float:
    span = max(float(row["high"]) - float(row["low"]), float(row["close"]) * 1e-9)
    if side == "short":
        wick = float(row["high"]) - max(float(row["open"]), float(row["close"]))
    else:
        wick = min(float(row["open"]), float(row["close"])) - float(row["low"])
    return max(0.0, wick / span)


def _setup_side(row: pd.Series, config: ConfirmedRejectionV2Config) -> Side | None:
    required = (
        "atr",
        "amf_histogram",
        "amf_regime",
        "upper_distance_atr",
        "lower_distance_atr",
        "upper_level",
        "lower_level",
    )
    if any(pd.isna(row.get(name)) for name in required):
        return None
    if float(row["atr"]) <= 0 or float(row["amf_regime"]) >= BASE_CONFIG.ranging_regime_max:
        return None
    short = (
        float(row["upper_distance_atr"]) <= BASE_CONFIG.max_level_distance_atr
        and float(row["high"]) >= float(row["upper_level"])
        and float(row["close"]) < float(row["upper_level"])
        and float(row["amf_histogram"]) < 0
    )
    long = (
        float(row["lower_distance_atr"]) <= BASE_CONFIG.max_level_distance_atr
        and float(row["low"]) <= float(row["lower_level"])
        and float(row["close"]) > float(row["lower_level"])
        and float(row["amf_histogram"]) > 0
    )
    side: Side | None = "short" if short else "long" if long else None
    if side is None or _wick_ratio(row, side) < config.minimum_rejection_wick_ratio:
        return None
    return side


def _confirmation_valid(
    setup: pd.Series,
    confirmation: pd.Series,
    side: Side,
    config: ConfirmedRejectionV2Config,
) -> bool:
    if _body_ratio(confirmation) < config.confirmation_body_ratio:
        return False
    midpoint = (float(setup["open"]) + float(setup["close"])) / 2.0
    if side == "long":
        return (
            float(confirmation["close"]) > float(confirmation["open"])
            and float(confirmation["close"]) > max(float(setup["close"]), midpoint)
            and float(confirmation["low"]) > float(setup["low"])
        )
    return (
        float(confirmation["close"]) < float(confirmation["open"])
        and float(confirmation["close"]) < min(float(setup["close"]), midpoint)
        and float(confirmation["high"]) < float(setup["high"])
    )


def _directional_bps(side: Side, entry: float, price: float) -> float:
    direction = 1.0 if side == "long" else -1.0
    return direction * (price / entry - 1.0) * 10_000.0


def replay_selection(
    one_hour: pd.DataFrame,
    four_hour: pd.DataFrame,
    fifteen_minute: pd.DataFrame,
    *,
    symbol: str,
    decision_end_exclusive: datetime,
    config: ConfirmedRejectionV2Config = DEFAULT_CONFIG,
) -> tuple[tuple[ConfirmedRejectionTrade, ...], dict[str, int]]:
    """Replay only decisions before ``decision_end_exclusive``.

    The caller is expected to supply no rows from the sealed tail.  The end
    timestamp is checked again here so accidental extra input cannot create a
    decision from it.
    """

    if decision_end_exclusive.tzinfo is None:
        raise ValueError("decision_end_exclusive must be timezone-aware")
    frame = build_mtf_amf_feature_frame(one_hour, four_hour, config=BASE_CONFIG)
    confirmation_frame = fifteen_minute.copy()
    confirmation_frame["timestamp"] = pd.to_datetime(
        confirmation_frame["timestamp"], utc=True
    ).astype("datetime64[ns, UTC]")
    confirmation_frame = (
        confirmation_frame.drop_duplicates("timestamp", keep="last")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    confirmation_frame = confirmation_frame.loc[
        confirmation_frame["timestamp"] < pd.Timestamp(decision_end_exclusive)
    ].reset_index(drop=True)
    counters: Counter[str] = Counter()
    trades: list[ConfirmedRejectionTrade] = []
    blocked_until_ts = pd.Timestamp.min.tz_localize(UTC)
    pos = BASE_CONFIG.warmup_bars
    while pos < len(frame):
        setup = frame.iloc[pos]
        setup_ts = pd.Timestamp(setup["timestamp"])
        observed_at = setup_ts + pd.Timedelta(hours=1)
        if observed_at.to_pydatetime() >= decision_end_exclusive:
            break
        counters["evaluated"] += 1
        if setup_ts < blocked_until_ts:
            counters["cooldown_or_active"] += 1
            pos += 1
            continue
        side = _setup_side(setup, config)
        if side is None:
            counters["setup_rejected"] += 1
            pos += 1
            continue
        counters["setups"] += 1
        candidates = confirmation_frame.loc[
            (confirmation_frame["timestamp"] >= observed_at)
            & (
                confirmation_frame["timestamp"]
                < observed_at
                + pd.Timedelta(minutes=15 * config.confirmation_window_bars)
            )
        ]
        confirmation_pos: int | None = None
        confirmation: pd.Series | None = None
        for candidate_pos, candidate in candidates.iterrows():
            if _confirmation_valid(setup, candidate, side, config):
                confirmation_pos = int(candidate_pos)
                confirmation = candidate
                break
        if confirmation is None or confirmation_pos is None:
            counters["confirmation_rejected"] += 1
            pos += 1
            continue
        counters["confirmed"] += 1
        entry_pos = confirmation_pos + 1
        if entry_pos >= len(confirmation_frame):
            counters["entry_unavailable"] += 1
            break
        entry_row = confirmation_frame.iloc[entry_pos]
        entry_ts = pd.Timestamp(entry_row["timestamp"])
        if entry_ts.to_pydatetime() >= decision_end_exclusive:
            counters["entry_outside_selection"] += 1
            break
        required_path_end = entry_ts + pd.Timedelta(
            minutes=15 * config.maximum_hold_bars
        )
        if required_path_end.to_pydatetime() > decision_end_exclusive:
            # A shortened path would bias the vertical barrier and could make
            # the selection window depend on how much future data happened to
            # be loaded.  Leave the setup unresolved instead.
            counters["insufficient_selection_path"] += 1
            break
        entry = float(entry_row["open"])
        atr_value = float(setup["atr"])
        stop = (
            float(setup["low"]) - config.stop_buffer_atr * atr_value
            if side == "long"
            else float(setup["high"]) + config.stop_buffer_atr * atr_value
        )
        stop_bps = abs(stop / entry - 1.0) * 10_000.0
        if not config.minimum_stop_bps <= stop_bps <= config.maximum_stop_bps:
            counters["stop_geometry_rejected"] += 1
            pos += 1
            continue
        target_bps = stop_bps * config.reward_risk
        if target_bps < config.round_trip_cost_bps * config.minimum_target_cost_multiple:
            counters["cost_geometry_rejected"] += 1
            pos += 1
            continue
        counters["entered"] += 1
        direction = 1.0 if side == "long" else -1.0
        target = entry * (1.0 + direction * target_bps / 10_000.0)
        # Re-anchor the stop to the exact distance used by the resolver.  This
        # avoids tiny asymmetry from division when the side is short.
        stop = entry * (1.0 - direction * stop_bps / 10_000.0)
        mfe = 0.0
        mae = 0.0
        resolved: tuple[int, pd.Series, float, str, bool] | None = None
        last = min(
            len(confirmation_frame) - 1,
            entry_pos + config.maximum_hold_bars - 1,
        )
        for path_pos in range(entry_pos, last + 1):
            bar = confirmation_frame.iloc[path_pos]
            favorable = (
                _directional_bps(side, entry, float(bar["high"]))
                if side == "long"
                else _directional_bps(side, entry, float(bar["low"]))
            )
            adverse = (
                -_directional_bps(side, entry, float(bar["low"]))
                if side == "long"
                else -_directional_bps(side, entry, float(bar["high"]))
            )
            mfe = max(mfe, favorable)
            mae = max(mae, adverse)
            stop_hit = float(bar["low"]) <= stop if side == "long" else float(bar["high"]) >= stop
            target_hit = (
                float(bar["high"]) >= target
                if side == "long"
                else float(bar["low"]) <= target
            )
            if stop_hit:
                resolved = (path_pos, bar, stop, "stop", target_hit)
                break
            if target_hit:
                resolved = (path_pos, bar, target, "target", False)
                break
        if resolved is None:
            bar = confirmation_frame.iloc[last]
            resolved = (last, bar, float(bar["close"]), "time_stop", False)
        exit_pos, exit_row, exit_price, reason, ambiguous = resolved
        gross = _directional_bps(side, entry, exit_price)
        trades.append(
            ConfirmedRejectionTrade(
                scanner_id=SCANNER_ID,
                symbol=symbol.upper(),
                side=side,
                setup_ts=pd.Timestamp(setup["timestamp"]).isoformat(),
                confirmation_ts=pd.Timestamp(confirmation["timestamp"]).isoformat(),
                entry_ts=pd.Timestamp(entry_row["timestamp"]).isoformat(),
                exit_ts=pd.Timestamp(exit_row["timestamp"]).isoformat(),
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                exit_price=exit_price,
                stop_bps=stop_bps,
                target_bps=target_bps,
                cost_bps=config.round_trip_cost_bps,
                gross_bps=gross,
                net_bps=gross - config.round_trip_cost_bps,
                mfe_bps=mfe,
                mae_bps=mae,
                hold_bars=exit_pos - entry_pos + 1,
                exit_reason=reason,
                same_bar_ambiguous=ambiguous,
                rejection_wick_ratio=_wick_ratio(setup, side),
                confirmation_body_ratio=_body_ratio(confirmation),
            )
        )
        cooldown_until = setup_ts + pd.Timedelta(hours=config.cooldown_bars + 1)
        exit_until = pd.Timestamp(exit_row["timestamp"]) + pd.Timedelta(minutes=15)
        blocked_until_ts = max(cooldown_until, exit_until)
        pos += 1
    return tuple(trades), dict(counters)


def _profit_factor(values: list[float]) -> float | None:
    if not values:
        return None
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    # JSON reports must remain standards-compliant.  A no-loss cell is marked
    # unavailable rather than serializing Infinity and is never sufficient by
    # itself to pass the overall sample-size gate.
    return gains / losses if losses else None


def _metrics(trades: list[ConfirmedRejectionTrade]) -> dict[str, Any]:
    net = [trade.net_bps for trade in trades]
    return {
        "trades": len(trades),
        "average_gross_bps": sum(trade.gross_bps for trade in trades) / len(trades)
        if trades
        else None,
        "average_net_bps": sum(net) / len(net) if net else None,
        "net_bps": sum(net),
        "profit_factor": _profit_factor(net),
        "win_rate": sum(value > 0 for value in net) / len(net) if net else None,
        "average_mfe_bps": sum(trade.mfe_bps for trade in trades) / len(trades)
        if trades
        else None,
        "average_mae_bps": sum(trade.mae_bps for trade in trades) / len(trades)
        if trades
        else None,
        "exit_reasons": dict(Counter(trade.exit_reason for trade in trades)),
        "same_bar_ambiguities": sum(trade.same_bar_ambiguous for trade in trades),
    }


def build_selection_report(
    candles_by_symbol: Mapping[
        str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]
    ],
    *,
    selection_end_exclusive: datetime = DEFAULT_SELECTION_END,
    untouched_start: datetime = DEFAULT_UNTOUCHED_START,
    config: ConfirmedRejectionV2Config = DEFAULT_CONFIG,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    if selection_end_exclusive >= untouched_start:
        raise ValueError("selection must end before untouched starts")
    all_trades: list[ConfirmedRejectionTrade] = []
    funnels: dict[str, dict[str, int]] = {}
    for symbol, (one_hour, four_hour, fifteen_minute) in candles_by_symbol.items():
        trades, funnel = replay_selection(
            one_hour,
            four_hour,
            fifteen_minute,
            symbol=symbol,
            decision_end_exclusive=selection_end_exclusive,
            config=config,
        )
        all_trades.extend(trades)
        funnels[symbol.upper()] = funnel
    ordered = sorted(all_trades, key=lambda trade: trade.entry_ts)
    midpoint = len(ordered) // 2
    market_groups: dict[str, list[ConfirmedRejectionTrade]] = defaultdict(list)
    for trade in ordered:
        market_groups[trade.symbol].append(trade)
    overall = _metrics(ordered)
    halves = [_metrics(ordered[:midpoint]), _metrics(ordered[midpoint:])]
    markets = {symbol: _metrics(rows) for symbol, rows in sorted(market_groups.items())}
    positive_markets = sum(
        (row["average_net_bps"] or -math.inf) > 0 for row in markets.values()
    )
    checks = {
        "minimum_trades": len(ordered) >= config.minimum_selection_trades,
        "minimum_market_samples": bool(markets)
        and all(row["trades"] >= config.minimum_trades_per_market for row in markets.values()),
        "gross_clears_cost": (overall["average_gross_bps"] or -math.inf)
        > config.round_trip_cost_bps,
        "average_net": (overall["average_net_bps"] or -math.inf)
        >= config.minimum_average_net_bps,
        "profit_factor": (overall["profit_factor"] or 0.0) >= config.minimum_profit_factor,
        "positive_markets": positive_markets >= 2,
        "positive_halves": all((row["average_net_bps"] or -math.inf) > 0 for row in halves),
    }
    contract = {
        "scanner_id": SCANNER_ID,
        "config": asdict(config),
        "entry": (
            "first qualifying completed 15m candle within 1h confirms failed retest; "
            "enter following 15m open"
        ),
        "exit": "stop beyond rejection extreme; 2R target; 12h vertical barrier; stop-first",
        "decision_data": "completed 15m, completed 1h, and completed 4h candles only",
    }
    contract_sha = hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    passed = all(checks.values())
    return {
        "schema_version": "vnedge.mtf_amf_confirmed_rejection.v2",
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(),
        "contract": contract,
        "contract_sha256": contract_sha,
        "selection_window": {
            "end_exclusive": selection_end_exclusive.isoformat(),
            "untouched_embargo_start": selection_end_exclusive.isoformat(),
        },
        "selection": {
            "metrics": overall,
            "markets": markets,
            "halves": halves,
            "positive_markets": positive_markets,
            "funnel": funnels,
            "gate": {"passed": passed, "checks": checks},
        },
        "untouched": {
            "start": untouched_start.isoformat(),
            "status": "sealed",
            "loaded": False,
            "predictions_computed": False,
            "trades_computed": False,
            "eligible_to_open": passed,
        },
        "policy": {
            "research_only": True,
            "registered_strategy": False,
            "paper_route": "absent",
            "order_route": "absent",
            "can_trade": False,
            "can_promote": False,
        },
        "trades": [trade.to_dict() for trade in ordered],
        "can_trade": False,
        "can_promote": False,
    }


def publish_report(payload: dict[str, Any], path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", dir=target.parent, prefix=target.name, suffix=".tmp", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--days", type=int, default=470)
    parser.add_argument("--selection-end", default=DEFAULT_SELECTION_END.isoformat())
    parser.add_argument("--untouched-start", default=DEFAULT_UNTOUCHED_START.isoformat())
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    selection_end = datetime.fromisoformat(args.selection_end).astimezone(UTC)
    untouched_start = datetime.fromisoformat(args.untouched_start).astimezone(UTC)
    # Fetch only enough data to warm the causal features and resolve selection
    # trades.  No request reaches the sealed-tail start.
    fetch_end = selection_end
    frames = {
        symbol.strip().upper(): (
            fetch_delta_public_candles(
                symbol.strip().upper(), "1h", days=args.days, now=fetch_end
            ),
            fetch_delta_public_candles(
                symbol.strip().upper(), "4h", days=args.days, now=fetch_end
            ),
            fetch_delta_public_candles(
                symbol.strip().upper(), "15m", days=args.days, now=fetch_end
            ),
        )
        for symbol in args.symbols.split(",")
        if symbol.strip()
    }
    payload = build_selection_report(
        frames,
        selection_end_exclusive=selection_end,
        untouched_start=untouched_start,
    )
    publish_report(payload, args.out)
    metrics = payload["selection"]["metrics"]
    print(
        f"{SCANNER_ID}: {metrics['trades']} trades, "
        f"avg_net={metrics['average_net_bps']}, PF={metrics['profit_factor']}; "
        f"selection_pass={payload['selection']['gate']['passed']}; can_trade=false"
    )


if __name__ == "__main__":
    main()
