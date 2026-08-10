"""Deterministic, causal regime and candle-feature calculations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean, pstdev

from vnedge.scalping.delta_engine.types import (
    Candle,
    L2Confirmation,
    Regime,
    RegimeProfile,
    SessionRegime,
    TrendStrength,
    VolatilityRegime,
)


def _ema(values: list[float], span: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (span + 1)
    out = values[0]
    for value in values[1:]:
        out = alpha * value + (1 - alpha) * out
    return out


def _true_ranges(rows: tuple[Candle, ...]) -> list[float]:
    out: list[float] = []
    for idx, candle in enumerate(rows):
        previous = rows[idx - 1].close if idx else candle.open
        out.append(max(candle.high - candle.low, abs(candle.high - previous), abs(candle.low - previous)))
    return out


def _efficiency(closes: list[float], window: int) -> float:
    sample = closes[-(window + 1) :]
    if len(sample) < window + 1:
        return 0.0
    path = sum(abs(sample[i] - sample[i - 1]) for i in range(1, len(sample)))
    return abs(sample[-1] - sample[0]) / path if path else 0.0


@dataclass(frozen=True)
class RegimeConfig:
    fast_ema: int = 12
    slow_ema: int = 36
    efficiency_window: int = 12
    trend_efficiency_min: float = 0.28
    expansion_ratio: float = 1.35
    funding_extreme_abs: float = 0.0005


@dataclass(frozen=True)
class RegimeProfileConfig:
    source_timeframe: str = "5m"
    adx_window: int = 14
    strong_trend_adx: float = 30.0
    range_adx_max: float = 22.0
    ema_fast: int = 20
    ema_slow: int = 50
    ema_separation_atr_min: float = 0.8
    atr_window: int = 14
    volatility_percentile_window: int = 200
    high_volatility_percentile: float = 0.75
    low_volatility_percentile: float = 0.30
    bollinger_window: int = 20
    funding_extreme_abs: float = 0.0005

    def __post_init__(self) -> None:
        if self.source_timeframe not in {"1m", "5m"}:
            raise ValueError("regime profile timeframe must be 1m or 5m")
        if self.ema_slow <= self.ema_fast:
            raise ValueError("regime profile slow EMA must exceed fast EMA")
        if self.strong_trend_adx <= self.range_adx_max:
            raise ValueError("strong trend ADX must exceed range ADX")
        if not 0 < self.low_volatility_percentile < self.high_volatility_percentile < 1:
            raise ValueError("invalid volatility percentile thresholds")


class RegimeEngine:
    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.config = config or RegimeConfig()

    def classify(
        self,
        candles: dict[str, tuple[Candle, ...]],
        *,
        funding_rate: float,
        funding_percentile: float = 0.5,
    ) -> Regime:
        if abs(funding_rate) >= self.config.funding_extreme_abs or (
            abs(funding_rate) >= self.config.funding_extreme_abs / 5
            and (funding_percentile <= 0.05 or funding_percentile >= 0.95)
        ):
            return Regime.FUNDING_EXTREME
        hourly = candles.get("1h", ())
        intraday = candles.get("15m", ())
        macro = candles.get("4h", ())
        if len(hourly) < self.config.slow_ema or len(intraday) < 24 or len(macro) < 12:
            return Regime.UNKNOWN
        ranges = _true_ranges(intraday)
        recent_atr = fmean(ranges[-6:])
        baseline_atr = fmean(ranges[-24:-6]) if ranges[-24:-6] else recent_atr
        if baseline_atr > 0 and recent_atr / baseline_atr >= self.config.expansion_ratio:
            return Regime.EXPANDING
        closes = [row.close for row in hourly]
        fast = _ema(closes[-self.config.slow_ema :], self.config.fast_ema)
        slow = _ema(closes[-self.config.slow_ema :], self.config.slow_ema)
        efficiency = _efficiency(closes, self.config.efficiency_window)
        if efficiency >= self.config.trend_efficiency_min:
            macro_up = macro[-1].close > macro[0].close
            macro_down = macro[-1].close < macro[0].close
            if fast > slow and macro_up:
                return Regime.TRENDING_UP
            if fast < slow and macro_down:
                return Regime.TRENDING_DOWN
        return Regime.QUIET


def _percentile_rank(values: list[float], current: float) -> float:
    if not values:
        return 0.5
    return (
        sum(value < current for value in values)
        + 0.5 * sum(value == current for value in values)
    ) / len(values)


def _rolling_mean(values: list[float], window: int) -> list[float]:
    if len(values) < window:
        return []
    running = sum(values[:window])
    output = [running / window]
    for index in range(window, len(values)):
        running += values[index] - values[index - window]
        output.append(running / window)
    return output


def _rolling_bb_widths(values: list[float], window: int) -> list[float]:
    if len(values) < window:
        return []
    running_sum = sum(values[:window])
    running_sq = sum(value * value for value in values[:window])
    output: list[float] = []
    for end in range(window - 1, len(values)):
        if end >= window:
            incoming = values[end]
            outgoing = values[end - window]
            running_sum += incoming - outgoing
            running_sq += incoming * incoming - outgoing * outgoing
        mean = running_sum / window
        variance = max(0.0, running_sq / window - mean * mean)
        output.append(4.0 * math.sqrt(variance) / mean if mean else 0.0)
    return output


def session_regime(ts_hour: int) -> SessionRegime:
    if 7 <= ts_hour < 13:
        return SessionRegime.EUROPE
    if 13 <= ts_hour < 16:
        return SessionRegime.OVERLAP
    if 16 <= ts_hour < 22:
        return SessionRegime.US
    return SessionRegime.ASIA


def regime_profile_flags(
    funding_rate: float,
    l2: L2Confirmation,
    config: RegimeProfileConfig,
) -> dict[str, bool]:
    return {
        "funding_extreme": abs(funding_rate) >= config.funding_extreme_abs,
        "l2_healthy": (
            l2.status in {"fresh", "aligned"} and l2.sequence_healthy is True
        ),
    }


def build_regime_profile(
    candles: dict[str, tuple[Candle, ...]],
    *,
    funding_rate: float,
    l2: L2Confirmation,
    config: RegimeProfileConfig | None = None,
) -> RegimeProfile:
    """Build orthogonal labels from immutable, already-closed candles only."""
    settings = config or RegimeProfileConfig()
    all_rows = candles.get(settings.source_timeframe, ())
    available_bars = len(all_rows)
    required_history = max(
        settings.ema_slow + 1,
        settings.adx_window + 1,
        settings.volatility_percentile_window + settings.atr_window - 1,
        settings.volatility_percentile_window + settings.bollinger_window - 1,
        21,
    )
    rows = all_rows[-required_history:]
    latest_ts = max(
        (series[-1].ts for series in candles.values() if series),
        default=None,
    )
    session = session_regime(latest_ts.hour if latest_ts is not None else 0)
    flags = regime_profile_flags(funding_rate, l2, settings)
    minimum = max(settings.ema_slow + 1, settings.adx_window + 1)
    if available_bars < minimum:
        return RegimeProfile(
            session=session,
            source_timeframe=settings.source_timeframe,
            flags=flags,
            metrics={"history_bars": float(available_bars)},
        )

    closes = [row.close for row in rows]
    true_ranges = _true_ranges(rows)
    atr_series = _rolling_mean(true_ranges, settings.atr_window)
    current_atr = atr_series[-1] if atr_series else 0.0
    current_close = closes[-1]
    atr_bps = current_atr / current_close * 10_000 if current_close else 0.0
    atr_bps_series = [
        value / closes[index + settings.atr_window - 1] * 10_000
        for index, value in enumerate(atr_series)
        if closes[index + settings.atr_window - 1] > 0
    ]
    atr_history = atr_bps_series[-settings.volatility_percentile_window :]
    atr_percentile = _percentile_rank(atr_history, atr_bps)

    widths = _rolling_bb_widths(closes, settings.bollinger_window)
    width_history = widths[-settings.volatility_percentile_window :]
    current_width = width_history[-1] if width_history else 0.0
    width_percentile = _percentile_rank(width_history, current_width)

    fast = _ema(closes[-settings.ema_slow :], settings.ema_fast)
    slow = _ema(closes[-settings.ema_slow :], settings.ema_slow)
    prior_fast = _ema(closes[-(settings.ema_slow + 1) : -1], settings.ema_fast)
    separation_atr = abs(fast - slow) / current_atr if current_atr > 0 else 0.0
    if fast > slow and fast > prior_fast:
        direction = "up"
    elif fast < slow and fast < prior_fast:
        direction = "down"
    else:
        direction = "flat"
    adx = _adx(rows[-max(30, settings.adx_window + 1) :], settings.adx_window)
    if (
        adx >= settings.strong_trend_adx
        and separation_atr >= settings.ema_separation_atr_min
        and direction != "flat"
    ):
        trend = TrendStrength.STRONG_TREND
    elif adx <= settings.range_adx_max:
        trend = TrendStrength.RANGE
    else:
        trend = TrendStrength.WEAK_TREND

    if atr_percentile >= settings.high_volatility_percentile:
        volatility = VolatilityRegime.HIGH
    elif atr_percentile <= settings.low_volatility_percentile:
        volatility = VolatilityRegime.LOW
    else:
        volatility = VolatilityRegime.MEDIUM
    return RegimeProfile(
        trend=trend,
        trend_direction=direction,
        volatility=volatility,
        session=session,
        source_timeframe=settings.source_timeframe,
        flags=flags,
        metrics={
            "history_bars": float(available_bars),
            "adx_14": adx,
            "ema_separation_atr": separation_atr,
            "atr_bps": atr_bps,
            "atr_percentile": atr_percentile,
            "bb_width_bps": current_width * 10_000,
            "bb_width_percentile": width_percentile,
            "efficiency_ratio": _efficiency(closes, 20),
        },
    )


def build_features(candles: dict[str, tuple[Candle, ...]]) -> dict[str, float]:
    """One feature definition shared by live context and replay."""
    rows = candles.get("1m", ()) or candles.get("5m", ())
    if not rows:
        return {}
    latest = rows[-1]
    previous = rows[:-1]
    close = latest.close
    candle_range = max(latest.range, close * 1e-9)
    body = latest.close - latest.open
    upper_wick = latest.high - max(latest.open, latest.close)
    lower_wick = min(latest.open, latest.close) - latest.low
    returns = [
        math.log(rows[i].close / rows[i - 1].close)
        for i in range(1, len(rows))
        if rows[i - 1].close > 0
    ]
    volumes = [row.volume for row in previous[-30:]]
    mean_volume = fmean(volumes) if volumes else latest.volume
    volume_std = pstdev(volumes) if len(volumes) > 1 else 0.0
    trs = _true_ranges(rows[-20:])
    atr = fmean(trs[-14:]) if trs else latest.range
    recent = previous[-12:]
    high_break = max((row.high for row in recent), default=latest.high)
    low_break = min((row.low for row in recent), default=latest.low)
    gains = [max(0.0, returns[i]) for i in range(max(0, len(returns) - 14), len(returns))]
    losses = [max(0.0, -returns[i]) for i in range(max(0, len(returns) - 14), len(returns))]
    avg_gain = fmean(gains) if gains else 0.0
    avg_loss = fmean(losses) if losses else 0.0
    rsi = 100.0 if avg_loss == 0 and avg_gain > 0 else (
        50.0 if avg_loss == avg_gain == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    )
    closes = [row.close for row in rows]
    close_mean = fmean(closes[-20:]) if closes else close
    close_std = pstdev(closes[-20:]) if len(closes) >= 2 else 0.0
    atr_history = _true_ranges(rows[-50:])
    atr_percentile = (
        (
            sum(value < atr for value in atr_history)
            + 0.5 * sum(value == atr for value in atr_history)
        )
        / len(atr_history)
        if atr_history
        else 0.5
    )
    relative_volume = latest.volume / mean_volume if mean_volume > 0 else 1.0
    close_location = (latest.close - latest.low) / candle_range
    volume_delta_proxy = latest.volume * (2.0 * close_location - 1.0)
    adx = _adx(rows[-30:], 14)
    hourly = candles.get("1h", ())
    four_hour = candles.get("4h", ())
    hourly_closes = [row.close for row in hourly]
    four_hour_closes = [row.close for row in four_hour]
    return {
        "return_1_bps": (latest.close / rows[-2].close - 1) * 10_000 if len(rows) > 1 else 0.0,
        "return_5_bps": (latest.close / rows[-6].close - 1) * 10_000 if len(rows) > 5 else 0.0,
        "atr_bps": atr / close * 10_000,
        "atr_percentile": atr_percentile,
        "bb_width_bps": 4.0 * close_std / close_mean * 10_000 if close_mean else 0.0,
        "adx_14": adx,
        "realized_vol_bps": pstdev(returns[-20:]) * 10_000 if len(returns) > 1 else 0.0,
        "body_ratio": abs(body) / candle_range,
        "body_direction": 1.0 if body > 0 else -1.0 if body < 0 else 0.0,
        "upper_wick_ratio": upper_wick / candle_range,
        "lower_wick_ratio": lower_wick / candle_range,
        "volume_z": (latest.volume - mean_volume) / volume_std if volume_std else 0.0,
        "relative_volume": relative_volume,
        "volume_delta_proxy": volume_delta_proxy,
        "prior_high_12": high_break,
        "prior_low_12": low_break,
        "breakout_up_bps": (latest.close / high_break - 1) * 10_000,
        "breakout_down_bps": (low_break / latest.close - 1) * 10_000,
        "ema_gap_bps": (_ema(closes[-30:], 9) / _ema(closes[-30:], 21) - 1) * 10_000,
        "ema_stack_up": float(
            _ema(closes[-30:], 9) > _ema(closes[-30:], 21) > _ema(closes[-30:], 30)
        ),
        "ema_stack_down": float(
            _ema(closes[-30:], 9) < _ema(closes[-30:], 21) < _ema(closes[-30:], 30)
        ),
        "context_1h_ema_gap_bps": (
            (_ema(hourly_closes, 12) / _ema(hourly_closes, 36) - 1) * 10_000
            if len(hourly_closes) >= 36
            else 0.0
        ),
        "context_4h_return_bps": (
            (four_hour_closes[-1] / four_hour_closes[0] - 1) * 10_000
            if len(four_hour_closes) >= 2
            else 0.0
        ),
        "rsi_14": rsi,
    }


def _adx(rows: tuple[Candle, ...], window: int) -> float:
    if len(rows) < window + 1:
        return 0.0
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr: list[float] = []
    for previous, current in zip(rows[-(window + 1) : -1], rows[-window:], strict=True):
        up = current.high - previous.high
        down = previous.low - current.low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    total_tr = sum(tr)
    if total_tr <= 0:
        return 0.0
    plus_di = 100.0 * sum(plus_dm) / total_tr
    minus_di = 100.0 * sum(minus_dm) / total_tr
    total_di = plus_di + minus_di
    return 100.0 * abs(plus_di - minus_di) / total_di if total_di else 0.0
