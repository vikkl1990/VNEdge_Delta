from __future__ import annotations

import time
import urllib.parse
from pathlib import Path

import numpy as np
import pandas as pd

from vnedge.research.delta_forced_flow_panel import (
    ForcedFlowPanelConfig,
    build_delta_forced_flow_panel,
    compute_causal_forced_flow_features,
    write_panel_artifacts,
)


def _base_panel(rows: int = 160) -> pd.DataFrame:
    timestamp = pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC")
    close = pd.Series(100.0 + np.arange(rows) * 0.01)
    frame = pd.DataFrame(
        {
            "timestamp": timestamp,
            "available_at": timestamp + pd.Timedelta(minutes=5),
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 10.0,
            "mark": close + 0.01,
            "index": close - 0.01,
            "oi": 1_000.0 + np.arange(rows, dtype=float),
        }
    )
    frame.loc[80, "oi"] -= 50.0
    frame.loc[80, ["high", "low"]] = [103.0, 97.0]
    return frame


def test_cascade_thresholds_do_not_use_future_rows() -> None:
    config = ForcedFlowPanelConfig(rolling_window=50, min_history=20)
    original = compute_causal_forced_flow_features(_base_panel(), config)
    altered = _base_panel()
    altered.loc[110:, "oi"] = altered.loc[110:, "oi"] * 100.0
    altered.loc[110:, "high"] = altered.loc[110:, "high"] * 3.0
    changed = compute_causal_forced_flow_features(altered, config)
    columns = [
        "oi_contraction_threshold",
        "range_expansion_threshold_bps",
        "forced_flow_score",
        "cascade_flag",
    ]
    pd.testing.assert_frame_equal(original.loc[:109, columns], changed.loc[:109, columns])


def test_missing_oi_is_explicitly_unavailable_not_false() -> None:
    config = ForcedFlowPanelConfig(rolling_window=50, min_history=20)
    frame = _base_panel().drop(columns="oi")
    result = compute_causal_forced_flow_features(frame, config)
    assert result["forced_flow_score"].isna().all()
    assert result["cascade_flag"].isna().all()
    assert result["cascade_side"].isna().all()


class FakeDeltaPanelApi:
    def __init__(self, *, omit_oi: bool = False) -> None:
        self.omit_oi = omit_oi
        self.calls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.calls.append(url)
        parsed = urllib.parse.urlparse(url)
        if parsed.path == "/v2/products/BTCUSD":
            return {
                "success": True,
                "result": {"spot_index": {"symbol": ".DEXBTUSD"}},
            }
        query = urllib.parse.parse_qs(parsed.query)
        symbol = query["symbol"][0]
        if symbol == "FUNDING:BTCUSD":
            return {
                "success": True,
                "result": [
                    {
                        "time": -3_600,
                        "open": 0.005,
                        "high": 0.005,
                        "low": 0.005,
                        "close": 0.005,
                    },
                    {
                        "time": 0,
                        "open": 0.01,
                        "high": 0.01,
                        "low": 0.01,
                        "close": 0.01,
                    }
                ],
            }
        if symbol == "OI:BTCUSD" and self.omit_oi:
            return {"success": True, "result": []}
        start = max(0, int(query["start"][0]))
        end = int(query["end"][0])
        rows = []
        for raw_start in range(start, end, 60):
            base = 100.0 + raw_start / 60_000.0
            if symbol == "MARK:BTCUSD":
                base += 0.1
            elif symbol == ".DEXBTUSD":
                base -= 0.1
            elif symbol == "OI:BTCUSD":
                base = 1_000.0 + raw_start / 60.0
            rows.append(
                {
                    "time": raw_start,
                    "open": base,
                    "high": base + 0.1,
                    "low": base - 0.1,
                    "close": base,
                    "volume": 10.0,
                }
            )
        return {"success": True, "result": rows}


async def test_builder_uses_documented_series_and_settled_funding_availability() -> None:
    api = FakeDeltaPanelApi()
    config = ForcedFlowPanelConfig(
        resolution="1m",
        rolling_window=50,
        min_history=20,
    )
    result = await build_delta_forced_flow_panel(
        "BTCUSD",
        start_s=0,
        end_s=7_200,
        config=config,
        http_get_json=api,
    )
    assert result.index_symbol == ".DEXBTUSD"
    assert result.quality["passed"] is True
    assert result.panel["mark"].notna().all()
    assert result.panel["index"].notna().all()
    assert result.panel["oi"].notna().all()
    before_settlement = result.panel["available_at"] < pd.Timestamp(3_600, unit="s", tz="UTC")
    after_settlement = result.panel["available_at"] >= pd.Timestamp(3_600, unit="s", tz="UTC")
    assert (result.panel.loc[before_settlement, "funding_rate"] == 0.005 / 100).all()
    assert (result.panel.loc[after_settlement, "funding_rate"] == 0.01 / 100).all()
    requested_symbols = {
        urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("symbol", [None])[0]
        for url in api.calls
    }
    assert {"BTCUSD", "MARK:BTCUSD", ".DEXBTUSD", "OI:BTCUSD", "FUNDING:BTCUSD"} <= (
        requested_symbols
    )


async def test_builder_fails_quality_when_oi_history_is_missing() -> None:
    api = FakeDeltaPanelApi(omit_oi=True)
    result = await build_delta_forced_flow_panel(
        "BTCUSD",
        start_s=0,
        end_s=7_200,
        config=ForcedFlowPanelConfig(
            resolution="1m",
            rolling_window=50,
            min_history=20,
        ),
        http_get_json=api,
    )
    assert result.quality["passed"] is False
    assert result.quality["cascade_proxy_available"] is False
    assert result.panel["cascade_flag"].isna().all()


async def test_artifacts_separate_and_hash_the_untouched_tail(tmp_path: Path) -> None:
    api = FakeDeltaPanelApi()
    config = ForcedFlowPanelConfig(
        resolution="1m",
        rolling_window=50,
        min_history=20,
        untouched_fraction=0.20,
    )
    result = await build_delta_forced_flow_panel(
        "BTCUSD",
        start_s=0,
        end_s=7_200,
        config=config,
        http_get_json=api,
    )
    manifest = write_panel_artifacts(result, config, tmp_path)
    assert manifest["selection"]["rows"] == 96
    assert manifest["untouched"]["rows"] == 24
    assert manifest["untouched"]["economics_evaluated"] is False
    assert manifest["untouched"]["sealed_for_single_evaluation"] is True
    assert len(manifest["untouched"]["sha256"]) == 64
    assert Path(manifest["manifest_path"]).exists()


async def test_future_end_is_rejected_before_any_market_request() -> None:
    api = FakeDeltaPanelApi()
    future = int(time.time()) + 60
    try:
        await build_delta_forced_flow_panel(
            "BTCUSD",
            start_s=future - 60,
            end_s=future,
            config=ForcedFlowPanelConfig(
                resolution="1m",
                rolling_window=50,
                min_history=20,
            ),
            http_get_json=api,
        )
    except ValueError as exc:
        assert "future" in str(exc)
    else:
        raise AssertionError("future panel end should fail closed")
    assert api.calls == []
