"""Canonical parsers for Delta India's compact public WebSocket schema.

The public endpoint uses short field names (``p``, ``s``, ``r``, ``fr``),
while older/internal fixtures used verbose names.  Live ingestion and replay
must share this adapter so research never depends on a test-only wire shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

AggressorSide = Literal["buy", "sell"]

MIN_CREDIBLE_EPOCH_US = 946_684_800_000_000  # 2000-01-01
MAX_CREDIBLE_EPOCH_US = 4_102_444_800_000_000  # 2100-01-01


def _number(message: Mapping[str, object], *keys: str) -> float:
    for key in keys:
        value = message.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Delta field {key} is not numeric") from exc
    raise ValueError(f"Delta message is missing numeric field: {'/'.join(keys)}")


def _optional_number(message: Mapping[str, object], *keys: str) -> float | None:
    if not any(message.get(key) is not None for key in keys):
        return None
    return _number(message, *keys)


def _optional_int(message: Mapping[str, object], *keys: str) -> int | None:
    value = _optional_number(message, *keys)
    return int(value) if value is not None else None


@dataclass(frozen=True)
class DeltaTimestamp:
    """A normalized epoch timestamp with its original wire semantics."""

    value_us: int | None
    raw_value: int | None
    source_key: str | None
    source_unit: str | None


def normalize_epoch_timestamp_us(value: object) -> tuple[int | None, str | None]:
    """Normalize seconds/milliseconds/microseconds/nanoseconds to epoch µs.

    Delta channels are not uniform about timestamp units.  Magnitude-based
    normalization is deterministic and rejects values outside 2000..2100
    rather than turning non-time counters into latency evidence.
    """

    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None, None
    absolute = abs(raw)
    if absolute >= 100_000_000_000_000_000:
        normalized, unit = raw // 1_000, "ns"
    elif absolute >= 100_000_000_000_000:
        normalized, unit = raw, "us"
    elif absolute >= 100_000_000_000:
        normalized, unit = raw * 1_000, "ms"
    elif absolute >= 100_000_000:
        normalized, unit = raw * 1_000_000, "s"
    else:
        return None, "unknown"
    if not MIN_CREDIBLE_EPOCH_US <= normalized <= MAX_CREDIBLE_EPOCH_US:
        return None, unit
    return normalized, unit


def delta_message_timestamp(
    message: Mapping[str, object],
    *,
    channel: str,
    publish: bool = False,
) -> DeltaTimestamp:
    """Return the channel-aware event or publish timestamp.

    Trades expose both trade time ``t`` and publish time ``ts``. Order-book
    updates use ``ts``/``t`` depending on the feed revision. Other channels
    are deliberately conservative so unrelated fields cannot masquerade as
    exchange time.
    """

    if publish:
        keys = ("ts", "publish_timestamp")
    elif channel == "trades":
        keys = ("t", "timestamp")
    elif channel in {"ob_updates", "ob_l2"}:
        keys = ("t", "lts", "ts", "timestamp")
    else:
        keys = ("timestamp", "t", "ts")
    for key in keys:
        if message.get(key) is None:
            continue
        try:
            raw = int(message[key])
        except (TypeError, ValueError):
            return DeltaTimestamp(None, None, key, "invalid")
        value_us, unit = normalize_epoch_timestamp_us(raw)
        return DeltaTimestamp(value_us, raw, key, unit)
    return DeltaTimestamp(None, None, None, None)


@dataclass(frozen=True)
class DeltaPublicTrade:
    price: float
    size: float
    aggressor_side: AggressorSide
    trade_timestamp_us: int | None
    publish_timestamp_us: int | None


def parse_public_trade(message: Mapping[str, object]) -> DeltaPublicTrade:
    """Decode both the compact production schema and legacy verbose fixtures.

    In Delta's compact feed ``r`` is the *buyer* role.  A taker buyer is an
    aggressive buy; a maker buyer means the seller was the aggressor.
    """

    price = _number(message, "p", "price")
    size = _number(message, "s", "size")
    if price <= 0 or size <= 0:
        raise ValueError("Delta trade price and size must be positive")

    buyer_role = message.get("r") or message.get("buyer_role")
    if buyer_role in {"t", "taker"}:
        side: AggressorSide = "buy"
    elif buyer_role in {"m", "maker"}:
        side = "sell"
    else:
        seller_role = message.get("seller_role")
        if seller_role == "taker":
            side = "sell"
        elif seller_role == "maker":
            side = "buy"
        elif message.get("side") in {"buy", "sell"}:
            side = str(message["side"])  # type: ignore[assignment]
        else:
            raise ValueError("Delta trade is missing a recognized aggressor role")

    return DeltaPublicTrade(
        price=price,
        size=size,
        aggressor_side=side,
        trade_timestamp_us=_optional_int(message, "t", "timestamp"),
        publish_timestamp_us=_optional_int(message, "ts"),
    )


def parse_funding_rate_decimal(message: Mapping[str, object]) -> float | None:
    """Return Delta's percentage funding field as a decimal rate."""

    percentage = _optional_number(message, "fr", "funding_rate")
    return percentage / 100.0 if percentage is not None else None


def parse_ticker_open_interest(
    message: Mapping[str, object], symbol: str
) -> float | None:
    """Extract contract open interest from the compact ticker payload."""

    rows = message.get("d")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            row_symbol = str(row.get("s") or row.get("symbol") or symbol).upper()
            if row_symbol != symbol.upper():
                continue
            oi = row.get("oi")
            if isinstance(oi, list | tuple) and oi:
                return _optional_number({"value": oi[0]}, "value")
            return _optional_number(row, "open_interest", "oi")
        return None
    return _optional_number(message, "open_interest", "oi")
