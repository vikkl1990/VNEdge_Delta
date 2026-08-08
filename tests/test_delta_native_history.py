"""Native Delta FUNDING: candle backfill — units, pagination, failure shapes."""

import urllib.parse

import pandas as pd
import pytest

from vnedge.data.delta_native_history import (
    fetch_delta_candle_history,
    fetch_delta_funding_history,
)

_HOUR_S = 3_600
_WINDOW_S = 1000 * _HOUR_S  # one 1h page as requested by the fetcher


def _params(url: str) -> dict[str, str]:
    query = urllib.parse.urlparse(url).query
    return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}


class _FakeApi:
    """Injectable http_get_json double; records every request's params."""

    def __init__(self, pages: list[dict] | None = None, echo_window: bool = False):
        self.calls: list[dict[str, str]] = []
        self._pages = pages or []
        self._echo_window = echo_window

    def __call__(self, url: str) -> dict:
        params = _params(url)
        self.calls.append(params)
        if self._echo_window:  # one candle at each requested window start
            return {
                "success": True,
                "result": [{"time": int(params["start"]), "close": 0.01}],
            }
        return self._pages[len(self.calls) - 1]


async def test_percent_to_fraction_epoch_seconds_and_sort():
    # real API shape: newest-first candles, close = funding in PERCENT,
    # time in epoch SECONDS. Output: ascending UTC timestamps, FRACTIONS.
    api = _FakeApi(
        pages=[
            {
                "success": True,
                "result": [
                    {"time": 7_200, "close": -0.028, "open": -0.03, "volume": None},
                    {"time": 3_600, "close": 0.01, "open": 0.01, "volume": None},
                ],
            }
        ]
    )
    df = await fetch_delta_funding_history("BTC/USD:USD", days=1, now_s=86_400, http_get_json=api)
    assert list(df.columns) == ["timestamp", "funding_rate"]
    assert list(df["timestamp"]) == [
        pd.Timestamp(7_200, unit="s", tz="UTC"),
        pd.Timestamp(10_800, unit="s", tz="UTC"),
    ]
    assert df["funding_rate"].tolist() == [0.01 / 100, -0.028 / 100]
    # dtype matches normalize_funding output (canonical funding frame)
    assert str(df["timestamp"].dtype) == "datetime64[ms, UTC]"
    # ccxt-style symbol mapped to Delta's native ticker + FUNDING: prefix
    assert api.calls[0]["symbol"] == "FUNDING:BTCUSD"
    assert api.calls[0]["resolution"] == "1h"
    assert (int(api.calls[0]["start"]), int(api.calls[0]["end"])) == (0, 86_400)


async def test_paginates_in_windows_below_the_response_cap():
    api = _FakeApi(echo_window=True)
    now = 1_700_000_000
    span = 90 * 86_400  # 2160 hourly candles > one 1000-candle page
    df = await fetch_delta_funding_history("BTC/USD:USD", days=90, now_s=now, http_get_json=api)
    assert len(api.calls) == 3
    first, second, third = api.calls
    assert int(first["start"]) == now - span
    assert int(first["end"]) == now - span + _WINDOW_S
    assert int(second["start"]) == int(first["end"])  # contiguous windows
    assert int(second["end"]) == int(second["start"]) + _WINDOW_S
    assert int(third["start"]) == int(second["end"])
    assert int(third["end"]) == now
    assert len(df) == 3
    assert df["timestamp"].is_monotonic_increasing


async def test_page_boundary_duplicates_are_deduped():
    dup = {"time": 3_600_000, "close": 0.02}
    api = _FakeApi(
        pages=[
            {"success": True, "result": [{"time": 3_600, "close": 0.01}, dup]},
                {"success": True, "result": [dup, {"time": 3_603_600, "close": 0.03}]},
        ]
    )
    df = await fetch_delta_funding_history("BTCUSD", days=60, now_s=60 * 86_400, http_get_json=api)
    assert len(df) == 3
    assert df["timestamp"].is_unique


async def test_forming_funding_candle_is_excluded_and_close_is_available_at_end():
    api = _FakeApi(
        pages=[
            {
                "success": True,
                "result": [
                    {"time": 3_600, "close": 0.01},
                    {"time": 7_200, "close": 0.02},
                ],
            }
        ]
    )

    df = await fetch_delta_funding_history("BTCUSD", days=1, now_s=9_000, http_get_json=api)

    assert len(df) == 1
    assert df.loc[0, "timestamp"] == pd.Timestamp(7_200, unit="s", tz="UTC")
    assert df.loc[0, "funding_rate"] == 0.01 / 100


async def test_empty_result_returns_typed_empty_frame():
    # unknown FUNDING: symbols return success=true with an empty result
    api = _FakeApi(pages=[{"success": True, "result": []}])
    df = await fetch_delta_funding_history("NOPE/USD:USD", days=1, now_s=86_400, http_get_json=api)
    assert df.empty
    assert list(df.columns) == ["timestamp", "funding_rate"]
    assert str(df["timestamp"].dtype) == "datetime64[ns, UTC]"


async def test_api_error_payload_raises():
    api = _FakeApi(pages=[{"success": False, "error": {"code": "bad_schema"}}])
    with pytest.raises(ValueError, match="delta candle API error"):
        await fetch_delta_funding_history("BTC/USD:USD", days=1, now_s=86_400, http_get_json=api)


async def test_malformed_rows_are_skipped():
    api = _FakeApi(
        pages=[
            {
                "success": True,
                "result": [
                    {"time": 3_600, "close": "0.01"},  # numeric string is fine
                    {"time": None, "close": 0.02},  # no timestamp
                    {"close": 0.5},  # missing time key
                    {"time": 7_200, "close": "junk"},  # unparseable close
                ],
            }
        ]
    )
    df = await fetch_delta_funding_history("BTC/USD:USD", days=1, now_s=86_400, http_get_json=api)
    assert len(df) == 1
    assert df.loc[0, "funding_rate"] == 0.01 / 100


async def test_unsupported_resolution_raises():
    with pytest.raises(ValueError, match="resolution"):
        await fetch_delta_funding_history(
            "BTC/USD:USD", days=1, resolution="7h", http_get_json=_FakeApi()
        )


async def test_public_candle_history_filters_forming_bar_and_normalizes():
    api = _FakeApi(
        pages=[
            {
                "success": True,
                "result": [
                    {"time": 0, "open": "100", "high": 102, "low": 99, "close": 101},
                    {"time": 60, "open": 101, "high": 103, "low": 100, "close": 102},
                ],
            }
        ]
    )
    frame = await fetch_delta_candle_history(
        "BTC/USD:USD",
        resolution="1m",
        start_s=0,
        end_s=90,
        http_get_json=api,
    )
    assert len(frame) == 1  # 00:01 bar is still forming at t=90
    assert frame.loc[0, "close"] == 101
    assert api.calls[0]["symbol"] == "BTCUSD"
