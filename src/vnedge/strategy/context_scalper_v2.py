"""Context scalper v2: 1h/15m context with 5m execution.

This is a VNEDGE-owned composite scanner, not a copied TradingView strategy.
It combines the two source-backed families that showed the strongest recent
watchlist behavior:

* ``vnedge_algo_ml_pro_v1`` for SuperTrend/BBP/ML-style flip entries.
* ``stealth_trail_bbp_v1`` for BBP pressure, displacement, structure and trail.

The extra contract this scanner adds is context discipline: every entry must
agree with completed 15m confirmation and 1h bias, and every emitted intent is
maker-first with taker fallback only when the projected net edge clears the
configured fee-wall buffer.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Literal

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.stealth_trail_bbp import (
    StealthTrailBBPParams,
    StealthTrailBBPScanner,
    add_stealth_trail_bbp_columns,
    stealth_trail_bbp_warmup_bars,
)
from vnedge.strategy.vnedge_algo_ml_pro import (
    VNEDGE_ALGO_ML_PRO_SIDES,
    VNEDGEAlgoMLProParams,
    add_vnedge_algo_ml_pro_columns,
    vnedge_algo_ml_pro_warmup_bars,
)


CONTEXT_SCALPER_V2_ID = "context_scalper_v2"
CONTEXT_SCALPER_V2_SIDES: tuple[str, ...] = ("long", "short")
ContextEngine = Literal["auto", "algo_ml", "stealth"]

_BASE_CANDLE_COLUMNS = {"timestamp", "open", "high", "low", "close", "volume"}


@dataclass(frozen=True)
class ContextScalperV2Params:
    """Frozen fee-aware context scanner parameters."""

    engine: ContextEngine = "auto"
    min_expected_net_edge_bps: float = 25.0
    min_fill_probability: float = 0.50
    maker_fill_probability: float = 0.60
    taker_extra_buffer_bps: float = 5.0
    allowed_sides: tuple[str, ...] = ()
    stealth_params: StealthTrailBBPParams = field(default_factory=StealthTrailBBPParams)
    algo_params: VNEDGEAlgoMLProParams = field(default_factory=VNEDGEAlgoMLProParams)

    @property
    def taker_fallback_threshold_bps(self) -> float:
        return self.min_expected_net_edge_bps + self.taker_extra_buffer_bps


@dataclass(frozen=True)
class _ContextCandidate:
    source: Literal["algo_ml", "stealth"]
    side: Literal["long", "short"]
    stop_price: float
    take_profit_price: float
    expected_edge_bps: float
    fill_probability: float
    source_reason: str


class ContextScalperV2(BaseStrategy):
    """Composite context scanner for live-data shadow/paper observation."""

    strategy_id = CONTEXT_SCALPER_V2_ID

    def __init__(
        self,
        funding: pd.DataFrame | None = None,
        *,
        params: ContextScalperV2Params | None = None,
        engine: ContextEngine | None = None,
        min_expected_net_edge_bps: float | None = None,
        min_fill_probability: float | None = None,
        maker_fill_probability: float | None = None,
        taker_extra_buffer_bps: float | None = None,
        allowed_sides: tuple[str, ...] | list[str] | None = None,
        stealth_params: StealthTrailBBPParams | dict | None = None,
        algo_params: VNEDGEAlgoMLProParams | dict | None = None,
        algo_use_ml_filter: bool | None = None,
        algo_ml_gate: float | None = None,
    ) -> None:
        base = params or ContextScalperV2Params()
        selected_engine = _validate_engine(base.engine if engine is None else engine)
        selected_sides = _validate_sides(
            tuple(base.allowed_sides if allowed_sides is None else allowed_sides)
        )
        min_edge = (
            base.min_expected_net_edge_bps
            if min_expected_net_edge_bps is None
            else float(min_expected_net_edge_bps)
        )
        min_fill = (
            base.min_fill_probability
            if min_fill_probability is None
            else float(min_fill_probability)
        )
        maker_fill = (
            base.maker_fill_probability
            if maker_fill_probability is None
            else float(maker_fill_probability)
        )
        taker_buffer = (
            base.taker_extra_buffer_bps
            if taker_extra_buffer_bps is None
            else float(taker_extra_buffer_bps)
        )

        raw_stealth = _coerce_stealth_params(
            base.stealth_params if stealth_params is None else stealth_params
        )
        raw_algo = _coerce_algo_params(base.algo_params if algo_params is None else algo_params)
        stealth_cfg = replace(
            raw_stealth,
            min_expected_net_edge_bps=min_edge,
            allowed_sides=selected_sides,
        )
        algo_cfg = replace(
            raw_algo,
            min_expected_net_edge_bps=min_edge,
            min_fill_probability=min_fill,
            allowed_sides=selected_sides,
            use_ml_filter=(
                raw_algo.use_ml_filter if algo_use_ml_filter is None else algo_use_ml_filter
            ),
            ml_gate=raw_algo.ml_gate if algo_ml_gate is None else float(algo_ml_gate),
        )
        self.params = ContextScalperV2Params(
            engine=selected_engine,
            min_expected_net_edge_bps=min_edge,
            min_fill_probability=min_fill,
            maker_fill_probability=max(0.0, min(1.0, maker_fill)),
            taker_extra_buffer_bps=taker_buffer,
            allowed_sides=selected_sides,
            stealth_params=stealth_cfg,
            algo_params=algo_cfg,
        )
        self.funding = funding
        self._stealth = StealthTrailBBPScanner(params=stealth_cfg)
        self.warmup_bars = max(
            stealth_trail_bbp_warmup_bars(stealth_cfg),
            vnedge_algo_ml_pro_warmup_bars(algo_cfg),
        )

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        base = add_stealth_trail_bbp_columns(candles, self.params.stealth_params)
        algo = add_vnedge_algo_ml_pro_columns(candles, self.params.algo_params)
        if len(base) != len(algo):
            raise ValueError("context_scalper_v2 child preparations must preserve row count")
        out = base.reset_index(drop=True)
        algo = algo.reset_index(drop=True)
        for column in algo.columns:
            if column in _BASE_CANDLE_COLUMNS:
                continue
            out[f"algo_{column}"] = algo[column]
        out["context_scalper_engine"] = self.params.engine
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        candidates: list[_ContextCandidate] = []
        if self.params.engine in ("stealth", "auto"):
            stealth = self._stealth_candidate(df, index, row)
            if stealth is not None:
                candidates.append(stealth)
        if self.params.engine in ("algo_ml", "auto"):
            candidates.extend(self._algo_candidates(row))
        if not candidates:
            return None
        selected = max(
            candidates,
            key=lambda candidate: (
                candidate.expected_edge_bps,
                candidate.fill_probability,
                1 if candidate.source == "algo_ml" else 0,
            ),
        )
        return SignalIntent(
            selected.side,
            stop_price=selected.stop_price,
            take_profit_price=selected.take_profit_price,
            reason=self._reason(selected, row),
        )

    def synthesize_exit_plan(
        self, df: pd.DataFrame, index: int, side: str, entry_price: float
    ) -> SignalIntent | None:
        if side not in CONTEXT_SCALPER_V2_SIDES:
            return None
        row = df.iloc[index]
        if self.params.engine in ("algo_ml", "auto"):
            stop = _safe_float(row.get(f"algo_stop_{side}"))
            target = _safe_float(row.get(f"algo_tp3_{side}"))
            if stop is not None and target is not None:
                return SignalIntent(
                    side,
                    stop_price=stop,
                    take_profit_price=target,
                    reason=(
                        "context_scalper_v2 rebuilt algo_ml trail plan; "
                        "SL_first; TP1/TP2/TP3 optional; BE_after_TP1"
                    ),
                )
        if self.params.engine in ("stealth", "auto"):
            rebuilt = self._stealth.synthesize_exit_plan(df, index, side, entry_price)
            if rebuilt is not None:
                return SignalIntent(
                    rebuilt.side,
                    stop_price=rebuilt.stop_price,
                    take_profit_price=rebuilt.take_profit_price,
                    reason=(
                        "context_scalper_v2 rebuilt stealth trail plan; "
                        "SL_first; TP1/TP2/TP3 optional; BE_after_TP1"
                    ),
                )
        return None

    def _stealth_candidate(
        self, df: pd.DataFrame, index: int, row: pd.Series
    ) -> _ContextCandidate | None:
        intent = self._stealth.signal(df, index)
        if intent is None:
            return None
        edge = _safe_float(row.get(f"expected_net_edge_bps_{intent.side}"))
        if edge is None or edge < self.params.min_expected_net_edge_bps:
            return None
        return _ContextCandidate(
            source="stealth",
            side=intent.side,
            stop_price=float(intent.stop_price),
            take_profit_price=float(intent.take_profit_price or row["close"]),
            expected_edge_bps=edge,
            fill_probability=self.params.maker_fill_probability,
            source_reason=intent.reason,
        )

    def _algo_candidates(self, row: pd.Series) -> list[_ContextCandidate]:
        out: list[_ContextCandidate] = []
        for side in VNEDGE_ALGO_ML_PRO_SIDES:
            if not self._algo_ready(row, side):
                continue
            edge = _safe_float(row.get(f"algo_expected_net_edge_bps_{side}"))
            fill = _safe_float(row.get(f"algo_fill_probability_{side}"))
            stop = _safe_float(row.get(f"algo_stop_{side}"))
            target = _safe_float(row.get(f"algo_tp3_{side}"))
            if edge is None or fill is None or stop is None or target is None:
                continue
            out.append(
                _ContextCandidate(
                    source="algo_ml",
                    side=side,
                    stop_price=stop,
                    take_profit_price=target,
                    expected_edge_bps=edge,
                    fill_probability=fill,
                    source_reason=self._algo_reason(row, side),
                )
            )
        return out

    def _algo_ready(self, row: pd.Series, side: str) -> bool:
        direction = 1 if side == "long" else -1
        edge = _safe_float(row.get(f"algo_expected_net_edge_bps_{side}"))
        fill = _safe_float(row.get(f"algo_fill_probability_{side}"))
        ml_score = _safe_float(row.get("algo_ml_score"))
        ml_gate = _safe_float(row.get("algo_effective_ml_gate"))
        trend_dir = _safe_float(row.get("algo_trend_dir"))
        return bool(
            self._side_allowed(side)
            and _flag(row, f"bias_1h_{side}")
            and _flag(row, f"confirm_15m_{side}")
            and _flag(row, "algo_raw_flip")
            and trend_dir is not None
            and int(trend_dir) == direction
            and _flag(row, f"algo_classic_filters_ok_{side}")
            and (
                not self.params.algo_params.use_ml_filter
                or (
                    ml_score is not None
                    and ml_gate is not None
                    and ml_score >= ml_gate
                )
            )
            and edge is not None
            and edge >= self.params.min_expected_net_edge_bps
            and fill is not None
            and fill >= self.params.min_fill_probability
        )

    def _side_allowed(self, side: str) -> bool:
        return not self.params.allowed_sides or side in self.params.allowed_sides

    def _algo_reason(self, row: pd.Series, side: str) -> str:
        return (
            f"algo_ml flip; ml={_fmt(row.get('algo_ml_score'))}; "
            f"htf={row.get('algo_htf_rule')}:{row.get('algo_htf_bias')}; "
            f"regime={row.get('algo_regime')}; "
            f"bbpStrength={_fmt(row.get('algo_bbp_strength'))}; "
            f"rr={_fmt(row.get(f'algo_rr_{side}'))}; "
            f"tp_ladder={_fmt(row.get(f'algo_tp1_{side}'))}/"
            f"{_fmt(row.get(f'algo_tp2_{side}'))}/"
            f"{_fmt(row.get(f'algo_tp3_{side}'))}"
        )

    def _reason(self, candidate: _ContextCandidate, row: pd.Series) -> str:
        taker_allowed = (
            candidate.expected_edge_bps >= self.params.taker_fallback_threshold_bps
        )
        return (
            f"context_scalper_v2 {candidate.side}; source={candidate.source}; "
            "mtf=5m_trigger/15m_confirm/1h_bias; route=maker_first; "
            f"expectedNet={candidate.expected_edge_bps:.1f}; "
            f"makerFillProbability={candidate.fill_probability:.2f}; "
            f"minEdge={self.params.min_expected_net_edge_bps:.1f}; "
            f"takerFallback={'allowed' if taker_allowed else 'blocked'}; "
            f"takerFallbackNeed={self.params.taker_fallback_threshold_bps:.1f}; "
            f"close={float(row['close']):.6g}; "
            "paperMargin=100; paperLeverage=25; paperNotional=2500; "
            "SL_first; trail_first; TP1/TP2/TP3 optional; BE_after_TP1; "
            f"source_reason={candidate.source_reason}"
        )


def _coerce_stealth_params(value: StealthTrailBBPParams | dict) -> StealthTrailBBPParams:
    if isinstance(value, StealthTrailBBPParams):
        return value
    params = dict(value)
    if "allowed_sides" in params:
        params["allowed_sides"] = _validate_sides(tuple(params["allowed_sides"]))
    return StealthTrailBBPParams(**params)


def _coerce_algo_params(value: VNEDGEAlgoMLProParams | dict) -> VNEDGEAlgoMLProParams:
    if isinstance(value, VNEDGEAlgoMLProParams):
        return value
    params = dict(value)
    if "allowed_sides" in params:
        params["allowed_sides"] = _validate_sides(tuple(params["allowed_sides"]))
    return VNEDGEAlgoMLProParams(**params)


def _validate_engine(value: str) -> ContextEngine:
    if value not in {"auto", "algo_ml", "stealth"}:
        raise ValueError("engine must be one of auto, algo_ml, stealth")
    return value  # type: ignore[return-value]


def _validate_sides(sides: tuple[str, ...]) -> tuple[str, ...]:
    invalid = sorted(set(sides) - set(CONTEXT_SCALPER_V2_SIDES))
    if invalid:
        raise ValueError(f"unsupported side(s): {invalid}")
    return sides


def _flag(row: pd.Series, name: str) -> bool:
    value = row.get(name)
    return False if _is_nan(value) else bool(value)


def _safe_float(value: object) -> float | None:
    if _is_nan(value):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _is_nan(value: object) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _fmt(value: object) -> str:
    number = _safe_float(value)
    if number is None:
        return "--"
    return f"{number:.6g}"
