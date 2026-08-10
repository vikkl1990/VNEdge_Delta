"""Production-shape Delta public WebSocket schema adapters."""

from __future__ import annotations

import pytest

from vnedge.exchange.delta_public_schema import (
    delta_message_timestamp,
    normalize_epoch_timestamp_us,
    parse_funding_rate_decimal,
    parse_public_trade,
    parse_ticker_open_interest,
)


@pytest.mark.parametrize(
    ("wire_value", "expected_unit"),
    [
        (1_783_532_640, "s"),
        (1_783_532_640_000, "ms"),
        (1_783_532_640_000_000, "us"),
        (1_783_532_640_000_000_000, "ns"),
    ],
)
def test_timestamp_units_normalize_to_the_same_epoch(
    wire_value: int, expected_unit: str
) -> None:
    normalized, unit = normalize_epoch_timestamp_us(wire_value)

    assert normalized == 1_783_532_640_000_000
    assert unit == expected_unit


def test_channel_timestamp_preserves_raw_semantics_and_rejects_counters() -> None:
    event = delta_message_timestamp(
        {"t": 1_783_532_640_000, "ts": 1_783_532_640_001},
        channel="trades",
    )
    publish = delta_message_timestamp(
        {"t": 1_783_532_640_000, "ts": 1_783_532_640_001},
        channel="trades",
        publish=True,
    )

    assert event.value_us == 1_783_532_640_000_000
    assert event.raw_value == 1_783_532_640_000
    assert event.source_key == "t" and event.source_unit == "ms"
    assert publish.value_us == 1_783_532_640_001_000
    assert delta_message_timestamp({"t": 42}, channel="trades").value_us is None


def test_compact_trade_buyer_role_maps_to_aggressor_side() -> None:
    buy = parse_public_trade(
        {"type": "trades", "p": "71234.5", "s": "0.2", "r": "t", "t": 11, "ts": 12}
    )
    sell = parse_public_trade(
        {"type": "trades", "p": "71234.0", "s": "0.3", "r": "m", "t": 13, "ts": 14}
    )

    assert buy.aggressor_side == "buy"
    assert sell.aggressor_side == "sell"
    assert buy.trade_timestamp_us == 11
    assert buy.publish_timestamp_us == 12


def test_compact_funding_and_ticker_fields_are_normalized() -> None:
    assert parse_funding_rate_decimal({"fr": "0.0100"}) == pytest.approx(0.0001)
    ticker = {"d": [{"s": "BTCUSD", "oi": ["1234.5", "0"]}]}
    assert parse_ticker_open_interest(ticker, "BTCUSD") == pytest.approx(1234.5)
    assert parse_ticker_open_interest(ticker, "ETHUSD") is None


def test_unrecognized_trade_role_fails_closed() -> None:
    with pytest.raises(ValueError, match="aggressor role"):
        parse_public_trade({"p": "100", "s": "1", "r": "unknown"})
