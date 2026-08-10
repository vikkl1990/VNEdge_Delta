"""Pine-parity replay for ``vnedge_algo_ml_pro_v1``.

This is a research-only answer to a very specific operator question: what does
the supplied TradingView script do if VNEDGE follows the same entry/exit
lifecycle?  It intentionally does not grant promotion or live permission.

The lifecycle mirrors the Pine source:

* signal enters on the confirmed signal-bar close;
* stop is checked first, by close only;
* TP1/TP2/TP3 are wick-touch markers, and only TP3 closes the position;
* trailing stop updates after TP/SL checks;
* a new opposite signal closes by reverse at the current close and opens again.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from typing import Literal

import pandas as pd

from vnedge.data.parquet_store import ParquetStore
from vnedge.exchange.delta_contracts import (
    DeltaContractSpec,
    fetch_india_contract_spec,
    size_delta_risk_trade,
)
from vnedge.scalping.parameter_registry import DEFAULT_SCALPER_PARAMETER_REGISTRY
from vnedge.strategy.vnedge_algo_ml_pro import (
    VNEDGE_ALGO_ML_PRO_ID,
    VNEDGEAlgoMLProParams,
    add_vnedge_algo_ml_pro_columns,
    vnedge_algo_ml_pro_warmup_bars,
)


CaptureMode = Literal["pine_tp3", "smart_ladder"]
SizingMode = Literal["fixed_notional", "delta_contract_risk"]


@dataclass(frozen=True)
class PineReplayConfig:
    """Research-only sizing and cost lens for Pine-parity replay."""

    paper_margin_usd: float = 100.0
    paper_leverage: float = 25.0
    account_equity_usd: float = 500.0
    risk_per_trade_pct: float = 1.0
    acknowledge_high_leverage: bool = False
    sizing_mode: SizingMode = "fixed_notional"
    delta_contract_spec: DeltaContractSpec | None = None
    fee_cost_bps: float | None = None
    capture_mode: CaptureMode = "pine_tp3"
    tp1_capture_fraction: float = 0.0
    tp2_capture_fraction: float = 0.0
    move_stop_to_be_after_tp1: bool = True
    lock_tp1_after_tp2: bool = True
    lookback_days: int = 30
    mark_open_at_end: bool = True
    include_trades: bool = False

    def __post_init__(self) -> None:
        if self.paper_margin_usd <= 0:
            raise ValueError("paper_margin_usd must be positive")
        if not 1.0 <= self.paper_leverage <= 30.0:
            raise ValueError("paper_leverage must be in [1, 30]")
        if self.account_equity_usd <= 0:
            raise ValueError("account_equity_usd must be positive")
        if self.risk_per_trade_pct <= 0:
            raise ValueError("risk_per_trade_pct must be positive")
        if self.sizing_mode not in ("fixed_notional", "delta_contract_risk"):
            raise ValueError("sizing_mode must be fixed_notional or delta_contract_risk")
        if self.sizing_mode == "delta_contract_risk" and self.delta_contract_spec is None:
            raise ValueError("delta_contract_risk sizing requires delta_contract_spec")
        if self.fee_cost_bps is not None and self.fee_cost_bps < 0:
            raise ValueError("fee_cost_bps cannot be negative")
        if self.capture_mode not in ("pine_tp3", "smart_ladder"):
            raise ValueError("capture_mode must be pine_tp3 or smart_ladder")
        for name, value in (
            ("tp1_capture_fraction", self.tp1_capture_fraction),
            ("tp2_capture_fraction", self.tp2_capture_fraction),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.tp1_capture_fraction + self.tp2_capture_fraction >= 1.0:
            raise ValueError("tp1/tp2 capture fractions must leave a runner")
        if self.lookback_days <= 0:
            raise ValueError("lookback_days must be positive")

    @property
    def paper_notional_usd(self) -> float:
        return self.paper_margin_usd * self.paper_leverage


@dataclass(frozen=True)
class PineReplayTrade:
    entry_ts: str
    exit_ts: str
    side: str
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    exit_reason: str
    hold_bars: int
    initial_stop: float
    final_stop: float
    tp1_price: float
    tp2_price: float | None
    tp3_price: float | None
    tp1_hit: bool
    tp2_hit: bool
    tp3_hit: bool
    r_multiple: float
    gross_bps: float
    fee_cost_bps: float
    fee_aware_net_bps: float
    visual_net_bps: float
    paper_visual_usd: float
    paper_fee_aware_usd: float
    paper_notional_usd: float = 0.0
    paper_margin_usd: float = 0.0
    paper_leverage: float = 0.0
    paper_contracts: int | None = None
    paper_base_quantity: float | None = None
    paper_liquidation_price: float | None = None
    sizing_mode: SizingMode = "fixed_notional"
    capture_mode: CaptureMode = "pine_tp3"
    remaining_fraction_closed_at_final: float = 1.0
    realized_gross_bps_before_final: float = 0.0
    open_at_end: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _OpenPosition:
    side: str
    direction: int
    entry_index: int
    entry_ts: str
    entry_price: float
    entry_sl: float
    sl_price: float
    tp1_price: float
    tp2_price: float | None
    tp3_price: float | None
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    remaining_fraction: float = 1.0
    realized_gross_bps: float = 0.0
    realized_r_multiple: float = 0.0
    paper_notional_usd: float = 0.0
    paper_margin_usd: float = 0.0
    paper_leverage: float = 0.0
    paper_contracts: int | None = None
    paper_base_quantity: float | None = None
    paper_liquidation_price: float | None = None


def run_vnedge_algo_ml_pro_pine_replay(
    candles: pd.DataFrame,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    params: VNEDGEAlgoMLProParams = VNEDGEAlgoMLProParams(),
    config: PineReplayConfig = PineReplayConfig(),
) -> dict:
    """Replay candles through the Pine-style TP/SL tracker."""

    fee_cost = _fee_cost_bps(exchange, config)
    prepared = add_vnedge_algo_ml_pro_columns(candles, params)
    trades = replay_prepared_vnedge_algo_ml_pro(
        prepared,
        params=params,
        config=config,
        fee_cost_bps=fee_cost,
        start_index=vnedge_algo_ml_pro_warmup_bars(params),
    )
    summary = summarize_pine_replay_trades(trades, config=config)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "truth_layer": "vnedge_algo_ml_pro_pine_replay_v1",
        "strategy_id": VNEDGE_ALGO_ML_PRO_ID,
        "source": "VNEDGE_ALGO_v6.0.1",
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "lookback_days": config.lookback_days,
        "paper_margin_usd": config.paper_margin_usd,
        "paper_leverage": config.paper_leverage,
        "paper_notional_usd": config.paper_notional_usd,
        "sizing_mode": config.sizing_mode,
        "account_equity_usd": config.account_equity_usd,
        "risk_per_trade_pct": config.risk_per_trade_pct,
        "acknowledge_high_leverage": config.acknowledge_high_leverage,
        "delta_contract_spec": (
            asdict(config.delta_contract_spec) if config.delta_contract_spec is not None else None
        ),
        "fee_cost_bps": fee_cost,
        "capture_mode": config.capture_mode,
        "bars": len(prepared),
        "policy": {
            "research_only": True,
            "can_trade": False,
            "can_promote": False,
            "matches_tradingview_lifecycle": True,
            "exact_pine_exit_lifecycle": config.capture_mode == "pine_tp3",
            "smart_capture_overlay": config.capture_mode == "smart_ladder",
            "visual_result_is_not_fee_aware": True,
        },
        "summary": summary,
        "trades": [t.to_dict() for t in trades] if config.include_trades else [],
        "trades_omitted": 0 if config.include_trades else len(trades),
    }


def replay_prepared_vnedge_algo_ml_pro(
    prepared: pd.DataFrame,
    *,
    params: VNEDGEAlgoMLProParams = VNEDGEAlgoMLProParams(),
    config: PineReplayConfig = PineReplayConfig(),
    fee_cost_bps: float = 0.0,
    start_index: int = 0,
) -> tuple[PineReplayTrade, ...]:
    """Replay an already-prepared frame. Public for unit tests."""

    if prepared.empty:
        return ()

    df = prepared.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    start = max(int(start_index), 0)
    trades: list[PineReplayTrade] = []
    position: _OpenPosition | None = None

    for index in range(start, len(df)):
        row = df.iloc[index]
        if position is not None:
            closed = _maybe_close_by_stop_or_tp3(
                position,
                row,
                index=index,
                config=config,
                fee_cost_bps=fee_cost_bps,
            )
            if closed is not None:
                trades.append(closed)
                position = None
            elif params.use_trailing:
                _update_trailing(position, row, params)

        signal_side = _signal_side(row)
        if signal_side is None:
            continue

        new_direction = 1 if signal_side == "long" else -1
        if position is not None:
            trades.append(
                _close_position(
                    position,
                    row,
                    index=index,
                    exit_reason="REVERSE",
                    exit_price=float(row["close"]),
                    config=config,
                    fee_cost_bps=fee_cost_bps,
                    open_at_end=False,
                )
            )
        position = _open_position(signal_side, new_direction, row, index, params, config)

    if position is not None and config.mark_open_at_end:
        row = df.iloc[-1]
        trades.append(
            _close_position(
                position,
                row,
                index=len(df) - 1,
                exit_reason="OPEN_MARK",
                exit_price=float(row["close"]),
                config=config,
                fee_cost_bps=fee_cost_bps,
                open_at_end=True,
            )
        )

    return tuple(trades)


def summarize_pine_replay_trades(
    trades: tuple[PineReplayTrade, ...],
    *,
    config: PineReplayConfig = PineReplayConfig(),
) -> dict:
    closed = [t for t in trades if not t.open_at_end]
    visual_values = [t.visual_net_bps for t in closed]
    fee_values = [t.fee_aware_net_bps for t in closed]
    r_values = [t.r_multiple for t in closed]
    reasons = Counter(t.exit_reason for t in trades)
    wins = sum(1 for t in closed if t.r_multiple >= 0.0)
    losses = len(closed) - wins
    gross_w = sum(t.r_multiple for t in closed if t.r_multiple >= 0.0)
    gross_l = sum(-t.r_multiple for t in closed if t.r_multiple < 0.0)
    fee_w = sum(t.fee_aware_net_bps for t in closed if t.fee_aware_net_bps > 0.0)
    fee_l = sum(-t.fee_aware_net_bps for t in closed if t.fee_aware_net_bps < 0.0)
    visual_w = sum(t.visual_net_bps for t in closed if t.visual_net_bps > 0.0)
    visual_l = sum(-t.visual_net_bps for t in closed if t.visual_net_bps < 0.0)
    hold_values = [t.hold_bars for t in closed]

    return {
        "bar_timing": {
            "entry_bar": "signal_close",
            "fixed_exit_wait_bars": None,
            "first_exit_check_delay_bars": 1,
            "self_learning_eval_horizon_bars": VNEDGEAlgoMLProParams().eval_horizon_bars,
            "self_learning_horizon_affects_trade_exit": False,
            "exit_wait_rule": "wait_until_sl_close_tp3_wick_reverse_or_open_mark",
            "stop_check": "close_only",
            "tp_check": "wick_touch_tp1_tp2_tp3",
            "tp3_closes_position": True,
            "tp1_tp2_are_markers_only": config.capture_mode == "pine_tp3",
        },
        "capture_mode": config.capture_mode,
        "smart_capture": {
            "enabled": config.capture_mode == "smart_ladder",
            "tp1_capture_fraction": (
                config.tp1_capture_fraction if config.capture_mode == "smart_ladder" else 0.0
            ),
            "tp2_capture_fraction": (
                config.tp2_capture_fraction if config.capture_mode == "smart_ladder" else 0.0
            ),
            "runner_fraction": (
                max(0.0, 1.0 - config.tp1_capture_fraction - config.tp2_capture_fraction)
                if config.capture_mode == "smart_ladder"
                else 1.0
            ),
            "move_stop_to_be_after_tp1": (
                config.move_stop_to_be_after_tp1 if config.capture_mode == "smart_ladder" else False
            ),
            "lock_tp1_after_tp2": (
                config.lock_tp1_after_tp2 if config.capture_mode == "smart_ladder" else False
            ),
        },
        "trades": len(trades),
        "closed_trades": len(closed),
        "open_marked_trades": len(trades) - len(closed),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": _pct(wins, len(closed)),
        "avg_r": _avg(r_values),
        "profit_factor_r": _pf(gross_w, gross_l),
        "visual_avg_bps": _avg(visual_values),
        "visual_profit_factor_bps": _pf(visual_w, visual_l),
        "fee_aware_avg_bps": _avg(fee_values),
        "fee_aware_profit_factor_bps": _pf(fee_w, fee_l),
        "visual_paper_usd": sum(t.paper_visual_usd for t in closed),
        "fee_aware_paper_usd": sum(t.paper_fee_aware_usd for t in closed),
        "paper_notional_usd": config.paper_notional_usd,
        "position_sizing": _position_sizing_summary(closed, config),
        "hold_bars": {
            "avg": _avg_int(hold_values),
            "median": median(hold_values) if hold_values else None,
            "min": min(hold_values) if hold_values else None,
            "max": max(hold_values) if hold_values else None,
            "by_exit_reason_avg": _hold_by_reason(closed),
        },
        "tp1_hits": sum(1 for t in trades if t.tp1_hit),
        "tp2_hits": sum(1 for t in trades if t.tp2_hit),
        "tp3_hits": sum(1 for t in trades if t.tp3_hit),
        "exit_reason_counts": dict(sorted(reasons.items())),
        "promotion_gate": {
            "min_closed_trades": 20,
            "min_fee_aware_avg_bps": 25.0,
            "min_fee_aware_pf": 1.5,
            "passed": bool(
                len(closed) >= 20
                and (mean(fee_values) if fee_values else float("-inf")) > 25.0
                and (_pf(fee_w, fee_l) or 0.0) > 1.5
            ),
        },
    }


def _maybe_close_by_stop_or_tp3(
    position: _OpenPosition,
    row: pd.Series,
    *,
    index: int,
    config: PineReplayConfig,
    fee_cost_bps: float,
) -> PineReplayTrade | None:
    close = float(row["close"])
    high = float(row["high"])
    low = float(row["low"])
    if position.direction == 1:
        if close <= position.sl_price:
            return _close_position(
                position,
                row,
                index=index,
                exit_reason="SL",
                exit_price=close,
                config=config,
                fee_cost_bps=fee_cost_bps,
            )
        if not position.tp1_hit and high >= position.tp1_price:
            position.tp1_hit = True
            if config.capture_mode == "smart_ladder":
                _capture_partial(position, position.tp1_price, config.tp1_capture_fraction)
                if config.move_stop_to_be_after_tp1:
                    position.sl_price = max(position.sl_price, position.entry_price)
        if (
            position.tp2_price is not None
            and not position.tp2_hit
            and high >= position.tp2_price
        ):
            position.tp2_hit = True
            if config.capture_mode == "smart_ladder":
                _capture_partial(position, position.tp2_price, config.tp2_capture_fraction)
                if config.lock_tp1_after_tp2:
                    position.sl_price = max(position.sl_price, position.tp1_price)
        if (
            position.tp3_price is not None
            and not position.tp3_hit
            and high >= position.tp3_price
        ):
            position.tp3_hit = True
            return _close_position(
                position,
                row,
                index=index,
                exit_reason="TP3",
                exit_price=position.tp3_price,
                config=config,
                fee_cost_bps=fee_cost_bps,
            )
        return None

    if close >= position.sl_price:
        return _close_position(
            position,
            row,
            index=index,
            exit_reason="SL",
            exit_price=close,
            config=config,
            fee_cost_bps=fee_cost_bps,
        )
    if not position.tp1_hit and low <= position.tp1_price:
        position.tp1_hit = True
        if config.capture_mode == "smart_ladder":
            _capture_partial(position, position.tp1_price, config.tp1_capture_fraction)
            if config.move_stop_to_be_after_tp1:
                position.sl_price = min(position.sl_price, position.entry_price)
    if (
        position.tp2_price is not None
        and not position.tp2_hit
        and low <= position.tp2_price
    ):
        position.tp2_hit = True
        if config.capture_mode == "smart_ladder":
            _capture_partial(position, position.tp2_price, config.tp2_capture_fraction)
            if config.lock_tp1_after_tp2:
                position.sl_price = min(position.sl_price, position.tp1_price)
    if (
        position.tp3_price is not None
        and not position.tp3_hit
        and low <= position.tp3_price
    ):
        position.tp3_hit = True
        return _close_position(
            position,
            row,
            index=index,
            exit_reason="TP3",
            exit_price=position.tp3_price,
            config=config,
            fee_cost_bps=fee_cost_bps,
        )
    return None


def _open_position(
    side: str,
    direction: int,
    row: pd.Series,
    index: int,
    params: VNEDGEAlgoMLProParams,
    config: PineReplayConfig,
) -> _OpenPosition | None:
    entry = float(row["close"])
    atr_value = _finite_or(float(row.get("atr_value", row.get("atr_base", 0.0))), 0.0)
    stop = _pine_stop_price(direction, entry, atr_value, row, params)
    sizing = _paper_sizing_for_entry(
        side=side,
        entry_price=entry,
        stop_price=stop,
        config=config,
    )
    if sizing is None:
        return None
    tp1 = entry + direction * params.tp1_atr_multiplier * atr_value
    tp2 = (
        entry + direction * params.tp2_atr_multiplier * atr_value
        if params.tp_levels >= 2
        else None
    )
    tp3 = (
        entry + direction * params.tp3_atr_multiplier * atr_value
        if params.tp_levels >= 3
        else None
    )
    return _OpenPosition(
        side=side,
        direction=direction,
        entry_index=index,
        entry_ts=_iso_ts(row["timestamp"]),
        entry_price=entry,
        entry_sl=stop,
        sl_price=stop,
        tp1_price=tp1,
        tp2_price=tp2,
        tp3_price=tp3,
        paper_notional_usd=sizing["notional_usd"],
        paper_margin_usd=sizing["margin_usd"],
        paper_leverage=sizing["leverage"],
        paper_contracts=sizing["contracts"],
        paper_base_quantity=sizing["base_quantity"],
        paper_liquidation_price=sizing["liquidation_price"],
    )


def _paper_sizing_for_entry(
    *,
    side: str,
    entry_price: float,
    stop_price: float,
    config: PineReplayConfig,
) -> dict[str, float | int | None] | None:
    if config.sizing_mode == "fixed_notional":
        return {
            "notional_usd": config.paper_notional_usd,
            "margin_usd": config.paper_margin_usd,
            "leverage": config.paper_leverage,
            "contracts": None,
            "base_quantity": config.paper_notional_usd / entry_price,
            "liquidation_price": None,
        }

    if config.delta_contract_spec is None:
        raise ValueError("delta_contract_risk sizing requires delta_contract_spec")
    sized = size_delta_risk_trade(
        account_equity_usd=config.account_equity_usd,
        risk_per_trade_pct=config.risk_per_trade_pct,
        entry_price=entry_price,
        stop_price=stop_price,
        side=side,
        requested_leverage=config.paper_leverage,
        acknowledge_high_leverage=config.acknowledge_high_leverage,
        spec=config.delta_contract_spec,
    )
    if not sized.approved:
        return None
    return {
        "notional_usd": sized.notional_usd,
        "margin_usd": sized.margin_usd,
        "leverage": sized.effective_leverage,
        "contracts": sized.contracts,
        "base_quantity": sized.base_quantity,
        "liquidation_price": sized.liquidation_price,
    }


def _close_position(
    position: _OpenPosition,
    row: pd.Series,
    *,
    index: int,
    exit_reason: str,
    exit_price: float,
    config: PineReplayConfig,
    fee_cost_bps: float,
    open_at_end: bool = False,
) -> PineReplayTrade:
    final_leg_gross_bps = (
        (float(exit_price) - position.entry_price)
        * position.direction
        / position.entry_price
        * 10_000.0
    )
    risk = abs(position.entry_price - position.entry_sl)
    final_leg_r_multiple = (
        (float(exit_price) - position.entry_price) * position.direction / risk
        if risk > 0
        else 0.0
    )
    final_fraction = max(0.0, min(position.remaining_fraction, 1.0))
    gross_bps = position.realized_gross_bps + final_fraction * final_leg_gross_bps
    r_multiple = position.realized_r_multiple + final_fraction * final_leg_r_multiple
    fee_aware = gross_bps - fee_cost_bps
    return PineReplayTrade(
        entry_ts=position.entry_ts,
        exit_ts=_iso_ts(row["timestamp"]),
        side=position.side,
        entry_index=position.entry_index,
        exit_index=index,
        entry_price=position.entry_price,
        exit_price=float(exit_price),
        exit_reason=exit_reason,
        hold_bars=max(index - position.entry_index, 0),
        initial_stop=position.entry_sl,
        final_stop=position.sl_price,
        tp1_price=position.tp1_price,
        tp2_price=position.tp2_price,
        tp3_price=position.tp3_price,
        tp1_hit=position.tp1_hit,
        tp2_hit=position.tp2_hit,
        tp3_hit=position.tp3_hit,
        r_multiple=r_multiple,
        gross_bps=gross_bps,
        fee_cost_bps=fee_cost_bps,
        fee_aware_net_bps=fee_aware,
        visual_net_bps=gross_bps,
        paper_visual_usd=gross_bps / 10_000.0 * position.paper_notional_usd,
        paper_fee_aware_usd=fee_aware / 10_000.0 * position.paper_notional_usd,
        paper_notional_usd=position.paper_notional_usd,
        paper_margin_usd=position.paper_margin_usd,
        paper_leverage=position.paper_leverage,
        paper_contracts=position.paper_contracts,
        paper_base_quantity=position.paper_base_quantity,
        paper_liquidation_price=position.paper_liquidation_price,
        sizing_mode=config.sizing_mode,
        capture_mode=config.capture_mode,
        remaining_fraction_closed_at_final=final_fraction,
        realized_gross_bps_before_final=position.realized_gross_bps,
        open_at_end=open_at_end,
    )


def _capture_partial(
    position: _OpenPosition,
    exit_price: float,
    fraction: float,
) -> None:
    if fraction <= 0.0 or position.remaining_fraction <= 0.0:
        return
    leg_fraction = min(fraction, position.remaining_fraction)
    leg_gross_bps = (
        (float(exit_price) - position.entry_price)
        * position.direction
        / position.entry_price
        * 10_000.0
    )
    risk = abs(position.entry_price - position.entry_sl)
    leg_r_multiple = (
        (float(exit_price) - position.entry_price) * position.direction / risk
        if risk > 0
        else 0.0
    )
    position.realized_gross_bps += leg_fraction * leg_gross_bps
    position.realized_r_multiple += leg_fraction * leg_r_multiple
    position.remaining_fraction = max(0.0, position.remaining_fraction - leg_fraction)


def _update_trailing(
    position: _OpenPosition,
    row: pd.Series,
    params: VNEDGEAlgoMLProParams,
) -> None:
    close = float(row["close"])
    atr_value = _finite_or(float(row.get("atr_value", row.get("atr_base", 0.0))), 0.0)
    atr_trail = (
        close - params.trail_atr_multiplier * atr_value
        if position.direction == 1
        else close + params.trail_atr_multiplier * atr_value
    )
    band_trail = _finite_or(float(row.get("st_band", float("nan"))), atr_trail)
    if params.trail_mode == "band":
        new_trail = band_trail
    elif params.trail_mode == "atr_to_band":
        new_trail = max(atr_trail, band_trail) if position.direction == 1 else min(
            atr_trail,
            band_trail,
        )
    else:
        new_trail = atr_trail
    if position.direction == 1:
        position.sl_price = max(position.sl_price, new_trail)
    else:
        position.sl_price = min(position.sl_price, new_trail)


def _pine_stop_price(
    direction: int,
    entry: float,
    atr_value: float,
    row: pd.Series,
    params: VNEDGEAlgoMLProParams,
) -> float:
    if params.sl_mode == "atr":
        return entry - params.sl_atr_multiplier * atr_value if direction == 1 else (
            entry + params.sl_atr_multiplier * atr_value
        )
    if params.sl_mode == "fixed_pct":
        return entry * (1.0 - params.sl_fixed_pct / 100.0) if direction == 1 else (
            entry * (1.0 + params.sl_fixed_pct / 100.0)
        )
    return _finite_or(float(row.get("st_band", float("nan"))), entry)


def _signal_side(row: pd.Series) -> str | None:
    if bool(row.get("confirmed_long", False)):
        return "long"
    if bool(row.get("confirmed_short", False)):
        return "short"
    return None


def _fee_cost_bps(exchange: str, config: PineReplayConfig) -> float:
    if config.fee_cost_bps is not None:
        return config.fee_cost_bps
    return DEFAULT_SCALPER_PARAMETER_REGISTRY.fee_profile(exchange).taker_round_trip_cost_bps


def _window(df: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    cutoff = out["timestamp"].max() - pd.Timedelta(days=lookback_days)
    return out[out["timestamp"] >= cutoff].reset_index(drop=True)


def _iso_ts(value: object) -> str:
    return pd.Timestamp(value).isoformat()


def _finite_or(value: float, fallback: float) -> float:
    return value if math.isfinite(value) else fallback


def _avg(values: list[float]) -> float | None:
    return mean(values) if values else None


def _avg_int(values: list[int]) -> float | None:
    return float(mean(values)) if values else None


def _hold_by_reason(trades: list[PineReplayTrade]) -> dict[str, float]:
    grouped: dict[str, list[int]] = {}
    for trade in trades:
        grouped.setdefault(trade.exit_reason, []).append(trade.hold_bars)
    return {reason: float(mean(values)) for reason, values in sorted(grouped.items())}


def _position_sizing_summary(trades: list[PineReplayTrade], config: PineReplayConfig) -> dict:
    notionals = [t.paper_notional_usd for t in trades if t.paper_notional_usd > 0.0]
    margins = [t.paper_margin_usd for t in trades if t.paper_margin_usd > 0.0]
    leverages = [t.paper_leverage for t in trades if t.paper_leverage > 0.0]
    contracts = [t.paper_contracts for t in trades if t.paper_contracts is not None]
    quantities = [t.paper_base_quantity for t in trades if t.paper_base_quantity is not None]
    return {
        "mode": config.sizing_mode,
        "requested_margin_usd": config.paper_margin_usd,
        "requested_leverage": config.paper_leverage,
        "requested_notional_usd": config.paper_notional_usd,
        "account_equity_usd": config.account_equity_usd,
        "risk_per_trade_pct": config.risk_per_trade_pct,
        "acknowledge_high_leverage": config.acknowledge_high_leverage,
        "actual_notional_usd_avg": _avg(notionals),
        "actual_notional_usd_min": min(notionals) if notionals else None,
        "actual_notional_usd_max": max(notionals) if notionals else None,
        "margin_usd_avg": _avg(margins),
        "effective_leverage_avg": _avg(leverages),
        "contracts_avg": _avg([float(c) for c in contracts]),
        "contracts_min": min(contracts) if contracts else None,
        "contracts_max": max(contracts) if contracts else None,
        "base_quantity_avg": _avg([float(q) for q in quantities]),
        "delta_contract_spec": (
            asdict(config.delta_contract_spec) if config.delta_contract_spec is not None else None
        ),
    }


def _pct(count: int, total: int) -> float:
    return count / total * 100.0 if total else 0.0


def _pf(gross_win: float, gross_loss: float) -> float | None:
    if gross_win <= 0 and gross_loss <= 0:
        return None
    if gross_loss <= 0:
        return 999.0
    return gross_win / gross_loss


def _render_summary(payload: dict) -> str:
    summary = payload["summary"]
    sizing = summary["position_sizing"]
    rows = [
        "VNEDGE Algo ML Pro Pine-Parity Replay",
        (
            f"{payload['exchange']} {payload['symbol']} {payload['timeframe']} "
            f"{payload['lookback_days']}d | paper ${payload['paper_margin_usd']:.0f} "
            f"x {payload['paper_leverage']:.0f} = ${payload['paper_notional_usd']:.0f}"
        ),
        (
            f"sizing mode: {sizing['mode']} | actual notional avg "
            f"${_fmt(sizing['actual_notional_usd_avg'])} | margin avg "
            f"${_fmt(sizing['margin_usd_avg'])}"
        ),
        f"fee cost: {payload['fee_cost_bps']:.2f} bps round trip",
        f"capture mode: {payload['capture_mode']}",
        (
            f"closed trades: {summary['closed_trades']} | "
            f"win% {summary['win_rate_pct']:.2f} | "
            f"PF(R) {_fmt(summary['profit_factor_r'])}"
        ),
        (
            f"visual avg: {_fmt(summary['visual_avg_bps'])} bps | "
            f"fee-aware avg: {_fmt(summary['fee_aware_avg_bps'])} bps"
        ),
        (
            f"visual USD: {_fmt(summary['visual_paper_usd'])} | "
            f"fee-aware USD: {_fmt(summary['fee_aware_paper_usd'])}"
        ),
        (
            "bars: fixed wait none | first check +"
            f"{summary['bar_timing']['first_exit_check_delay_bars']} | "
            f"avg hold {_fmt(summary['hold_bars']['avg'])} | "
            f"median hold {_fmt(summary['hold_bars']['median'])}"
        ),
        f"exits: {summary['exit_reason_counts']}",
        f"promotion gate passed: {summary['promotion_gate']['passed']}",
    ]
    return "\n".join(rows)


def _fmt(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="research-only Pine-parity replay for vnedge_algo_ml_pro_v1"
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--exchange", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--paper-margin-usd", type=float, default=100.0)
    parser.add_argument("--paper-leverage", type=float, default=25.0)
    parser.add_argument("--account-equity-usd", type=float, default=500.0)
    parser.add_argument("--risk-per-trade-pct", type=float, default=1.0)
    parser.add_argument("--acknowledge-high-leverage", action="store_true")
    parser.add_argument(
        "--sizing-mode",
        choices=("fixed_notional", "delta_contract_risk"),
        default="fixed_notional",
    )
    parser.add_argument(
        "--delta-live-product-spec",
        action="store_true",
        help="Fetch Delta India product metadata for --symbol and apply contract sizing",
    )
    parser.add_argument("--delta-contract-value", type=float)
    parser.add_argument("--delta-contract-unit-currency")
    parser.add_argument("--delta-initial-margin-pct", type=float)
    parser.add_argument("--delta-maintenance-margin-pct", type=float)
    parser.add_argument("--delta-min-contracts", type=int, default=1)
    parser.add_argument("--fee-cost-bps", type=float)
    parser.add_argument(
        "--capture-mode",
        choices=("pine_tp3", "smart_ladder"),
        default="pine_tp3",
        help="pine_tp3 matches TradingView exactly; smart_ladder captures TP1/TP2",
    )
    parser.add_argument("--tp1-capture-fraction", type=float, default=0.0)
    parser.add_argument("--tp2-capture-fraction", type=float, default=0.0)
    parser.add_argument("--no-be-after-tp1", action="store_true")
    parser.add_argument("--no-lock-tp1-after-tp2", action="store_true")
    parser.add_argument("--include-trades", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    delta_spec = _delta_contract_spec_from_args(args)

    config = PineReplayConfig(
        paper_margin_usd=args.paper_margin_usd,
        paper_leverage=args.paper_leverage,
        account_equity_usd=args.account_equity_usd,
        risk_per_trade_pct=args.risk_per_trade_pct,
        acknowledge_high_leverage=args.acknowledge_high_leverage,
        sizing_mode=args.sizing_mode,
        delta_contract_spec=delta_spec,
        fee_cost_bps=args.fee_cost_bps,
        capture_mode=args.capture_mode,
        tp1_capture_fraction=args.tp1_capture_fraction,
        tp2_capture_fraction=args.tp2_capture_fraction,
        move_stop_to_be_after_tp1=not args.no_be_after_tp1,
        lock_tp1_after_tp2=not args.no_lock_tp1_after_tp2,
        lookback_days=args.lookback_days,
        include_trades=args.include_trades,
    )
    store = ParquetStore(args.data_root)
    candles = _window(
        store.read_candles(args.exchange, args.symbol, args.timeframe),
        args.lookback_days,
    )
    payload = run_vnedge_algo_ml_pro_pine_replay(
        candles,
        exchange=args.exchange,
        symbol=args.symbol,
        timeframe=args.timeframe,
        config=config,
    )
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_render_summary(payload))
    return 0


def _delta_contract_spec_from_args(args: argparse.Namespace) -> DeltaContractSpec | None:
    if args.delta_live_product_spec:
        return fetch_india_contract_spec(args.symbol)
    if args.delta_contract_value is None:
        return None
    return DeltaContractSpec(
        symbol=args.symbol.replace("/", "").replace(":USD", ""),
        contract_value=args.delta_contract_value,
        contract_unit_currency=args.delta_contract_unit_currency or "",
        initial_margin_pct=args.delta_initial_margin_pct,
        maintenance_margin_pct=args.delta_maintenance_margin_pct,
        min_contracts=args.delta_min_contracts,
    )


if __name__ == "__main__":
    raise SystemExit(main())
