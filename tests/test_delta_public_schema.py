"""Production-shape Delta public WebSocket schema adapters."""

from __future__ import annotations

import pytest

from vnedge.exchange.delta_public_schema import (
    parse_funding_rate_decimal,
    parse_public_trade,
    parse_ticker_open_interest,
)


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
