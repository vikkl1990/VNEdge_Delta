"""Multi-timeframe scalper context stack."""

from datetime import UTC, datetime

import pandas as pd

from vnedge.research.scalper_context import build_context_stack


def candles(freq: str, closes: list[float]) -> pd.DataFrame:
    timestamps = pd.date_range(
        datetime(2026, 7, 5, tzinfo=UTC),
        periods=len(closes),
        freq=freq,
    )
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": closes,
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes],
        "close": closes,
        "volume": [100.0] * len(closes),
    })


def test_context_stack_tags_side_alignment():
    frames = {
        "4h": candles("4h", [100, 101, 102, 103, 104, 105]),
        "1h": candles("1h", [100, 101, 102, 103, 104, 105]),
        "15m": candles("15min", [100, 101, 102, 103, 104, 105]),
        "1m": candles("1min", [100, 101, 102, 103, 104, 105]),
    }
    at_ms = int(frames["4h"]["timestamp"].iloc[-1].timestamp() * 1000)

    stack = build_context_stack(frames, at_ms=at_ms)

    assert stack.coverage == 4
    assert stack.score > 0
    assert stack.tag_for_side("buy") == "aligned"
    assert stack.tag_for_side("sell") == "hostile"


def test_context_stack_requires_coverage_before_alignment():
    stack = build_context_stack({}, at_ms=1_750_000_000_000)

    assert stack.coverage == 0
    assert stack.tag_for_side("buy") == "missing"
