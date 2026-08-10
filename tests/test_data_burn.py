"""Data-burn registry — overlap detection, fail-closed judgments, hash chain,
window-keyed auto-explore dedupe, CLI."""

import json
import types

import pandas as pd
import pytest

from vnedge.backtest.metrics import BacktestMetrics
from vnedge.backtest.walk_forward import WalkForwardResult, WindowResult
from vnedge.execution.fill_ledger import verify_chain
from vnedge.research import continuous_research as cr
from vnedge.research import data_burn

LANE = dict(
    strategy_id="funding_mean_reversion_v1",
    symbol="BTC/USDT:USDT",
    exchange="binanceusdm",
)


@pytest.fixture
def registry(tmp_path):
    return tmp_path / "burn_registry.jsonl"


# --------------------------------------------------------------------------
# Overlap detection
# --------------------------------------------------------------------------


def test_overlaps_partial_and_boundary(registry):
    data_burn.record_judgment(
        **LANE, window_start="2024-07-03", window_end="2025-07-03",
        verdict="PASS", note="round 3", path=registry,
    )
    def hits(start, end):
        return data_burn.overlaps(**LANE, start=start, end=end, path=registry)

    assert len(hits("2024-07-03", "2025-07-03")) == 1  # exact
    assert len(hits("2025-01-01", "2025-12-31")) == 1  # partial (right)
    assert len(hits("2024-01-01", "2024-08-01")) == 1  # partial (left)
    assert len(hits("2024-09-01", "2024-10-01")) == 1  # contained
    assert len(hits("2023-01-01", "2026-01-01")) == 1  # containing
    assert len(hits("2025-07-03", "2026-01-01")) == 1  # boundary inclusive
    assert hits("2025-07-04", "2026-01-01") == []      # adjacent, no overlap
    assert hits("2023-01-01", "2024-07-02") == []


def test_overlaps_is_lane_scoped(registry):
    data_burn.record_judgment(
        **LANE, window_start="2024-07-03", window_end="2025-07-03",
        verdict="PASS", path=registry,
    )
    for other in (
        {**LANE, "strategy_id": "trend_continuation_v1"},
        {**LANE, "symbol": "ETH/USDT:USDT"},
        {**LANE, "exchange": "bybit"},
    ):
        assert data_burn.overlaps(
            **other, start="2024-07-03", end="2025-07-03", path=registry
        ) == []


def test_exploratory_burns_count_as_burned(registry):
    data_burn.record_burn(
        **LANE, window_start="2025-07-02", window_end="2026-07-02",
        verdict="REJECT", note="auto variant", path=registry,
    )
    with pytest.raises(data_burn.BurnedDataError):
        data_burn.assert_untouched(
            **LANE, start="2026-01-01", end="2026-06-01", path=registry
        )


def test_assert_untouched_raises_with_evidence(registry):
    data_burn.record_judgment(
        **LANE, window_start="2024-07-03", window_end="2025-07-03",
        verdict="PASS", note="round 3", path=registry,
    )
    data_burn.record_burn(
        **LANE, window_start="2025-07-02", window_end="2026-07-02",
        verdict="REJECT", path=registry,
    )
    with pytest.raises(data_burn.BurnedDataError) as err:
        data_burn.assert_untouched(
            **LANE, start="2025-01-01", end="2026-01-01", path=registry
        )
    assert len(err.value.records) == 2
    assert {r["kind"] for r in err.value.records} == {"judgment", "exploratory_burn"}
    assert "round 3" in str(err.value)
    # Clean window on the same lane passes.
    data_burn.assert_untouched(
        **LANE, start="2023-01-01", end="2024-07-02", path=registry
    )


def test_accepts_pandas_timestamps(registry):
    data_burn.record_burn(
        **LANE,
        window_start=pd.Timestamp("2025-01-01", tz="UTC"),
        window_end=pd.Timestamp("2025-06-01", tz="UTC"),
        path=registry,
    )
    assert len(data_burn.overlaps(
        **LANE, start=pd.Timestamp("2025-03-01", tz="UTC"),
        end=pd.Timestamp("2025-09-01", tz="UTC"), path=registry,
    )) == 1


def test_rejects_inverted_window_and_bad_kind(registry):
    with pytest.raises(ValueError):
        data_burn.record_burn(
            **LANE, window_start="2025-06-01", window_end="2025-01-01",
            path=registry,
        )
    with pytest.raises(ValueError):
        data_burn._record(
            "oops", "s", "BTC/USDT:USDT", "binanceusdm",
            "2025-01-01", "2025-02-01", "X", path=registry,
        )


# --------------------------------------------------------------------------
# judge_untouched — the fail-closed judgment wrapper
# --------------------------------------------------------------------------


def test_judge_untouched_runs_once_then_refuses(registry):
    calls = []

    def run():
        calls.append(1)
        return "PASS"

    verdict = data_burn.judge_untouched(
        **LANE, window_start="2023-07-03", window_end="2024-07-03",
        run=run, note="round 4", path=registry,
    )
    assert verdict == "PASS"
    assert calls == [1]
    records = data_burn.read_records(registry)
    assert len(records) == 1
    assert records[0]["kind"] == "judgment"
    assert records[0]["verdict"] == "PASS"
    assert records[0]["note"] == "round 4"

    # Second attempt on the (now burned) window refuses WITHOUT running.
    with pytest.raises(data_burn.BurnedDataError):
        data_burn.judge_untouched(
            **LANE, window_start="2023-07-03", window_end="2024-07-03",
            run=run, path=registry,
        )
    assert calls == [1]


def test_judge_untouched_burns_window_even_when_run_raises(registry):
    def run():
        raise RuntimeError("backtest exploded")

    with pytest.raises(RuntimeError):
        data_burn.judge_untouched(
            **LANE, window_start="2023-07-03", window_end="2024-07-03",
            run=run, path=registry,
        )
    records = data_burn.read_records(registry)
    assert len(records) == 1
    assert records[0]["verdict"] == "ERROR"
    assert "backtest exploded" in records[0]["note"]
    # The window is burned: a retry must refuse.
    with pytest.raises(data_burn.BurnedDataError):
        data_burn.assert_untouched(
            **LANE, start="2023-07-03", end="2024-07-03", path=registry
        )


# --------------------------------------------------------------------------
# Hash chain
# --------------------------------------------------------------------------


def test_chain_verifies_and_detects_tamper(registry):
    data_burn.record_judgment(
        **LANE, window_start="2024-07-03", window_end="2025-07-03",
        verdict="PASS", path=registry,
    )
    data_burn.record_burn(
        **LANE, window_start="2025-07-02", window_end="2026-07-02",
        verdict="REJECT", path=registry,
    )
    assert verify_chain(registry).ok
    assert data_burn.main(["--registry", str(registry), "verify"]) == 0

    lines = registry.read_text().splitlines()
    tampered = json.loads(lines[0])
    tampered["verdict"] = "REJECT"  # rewrite history
    lines[0] = json.dumps(tampered, sort_keys=True)
    registry.write_text("\n".join(lines) + "\n")

    report = verify_chain(registry)
    assert not report.ok and report.first_bad_line == 1
    assert data_burn.main(["--registry", str(registry), "verify"]) == 1
    # Appending to a broken chain is refused.
    with pytest.raises(ValueError):
        data_burn.BurnRegistry(registry)


def test_window_fingerprint_day_and_params_sensitivity():
    fp = data_burn.window_fingerprint("2026-07-09T13:00:00+00:00", {"a": 1})
    assert fp == data_burn.window_fingerprint("2026-07-09T23:00:00+00:00", {"a": 1})
    assert fp.startswith("20260709|")
    assert fp != data_burn.window_fingerprint("2026-07-10T01:00:00+00:00", {"a": 1})
    assert fp != data_burn.window_fingerprint("2026-07-09T13:00:00+00:00", {"a": 2})


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cli(registry, *args):
    return data_burn.main(["--registry", str(registry), *args])


LANE_ARGS = ("--strategy", "funding_mean_reversion_v1",
             "--symbol", "BTC/USDT:USDT", "--exchange", "binanceusdm")


def test_cli_check_and_register(registry, capsys):
    assert cli(registry, "check", *LANE_ARGS,
               "--start", "2024-07-03", "--end", "2025-07-03") == 0
    assert "UNTOUCHED" in capsys.readouterr().out

    assert cli(registry, "register", "--kind", "judgment", *LANE_ARGS,
               "--start", "2024-07-03", "--end", "2025-07-03",
               "--verdict", "PASS", "--note", "round 3") == 0
    out = capsys.readouterr().out
    assert "RECORDED" in out and "commit" in out

    assert cli(registry, "check", *LANE_ARGS,
               "--start", "2025-01-01", "--end", "2025-12-31") == 1
    assert "BURNED" in capsys.readouterr().out

    # A new judgment on burned data is refused; --allow-burned is for
    # backfilling historical facts only.
    assert cli(registry, "register", "--kind", "judgment", *LANE_ARGS,
               "--start", "2025-01-01", "--end", "2025-12-31",
               "--verdict", "PASS") == 1
    assert "REFUSED" in capsys.readouterr().out
    assert cli(registry, "register", "--kind", "judgment", *LANE_ARGS,
               "--start", "2025-01-01", "--end", "2025-12-31",
               "--verdict", "REJECT", "--allow-burned") == 0
    # Exploratory burns are facts — never refused.
    assert cli(registry, "register", "--kind", "exploratory_burn", *LANE_ARGS,
               "--start", "2025-01-01", "--end", "2025-12-31",
               "--verdict", "REJECT") == 0
    assert len(data_burn.read_records(registry)) == 3


# --------------------------------------------------------------------------
# auto_explore: window-keyed dedupe + exploratory burn trail
# --------------------------------------------------------------------------

BASE = 1_750_000_000_000


def ts(i):
    return pd.Timestamp(BASE + i * 3_600_000, unit="ms", tz="UTC")


def metrics(num_trades=6, net=15.0):
    return BacktestMetrics(
        num_trades=num_trades, skipped_by_sizing=0, net_profit_usd=net,
        return_pct=3.0, max_drawdown_pct=2.0, sharpe=1.0, sortino=1.1,
        profit_factor=1.5, win_rate_pct=60.0, avg_win_usd=6.0,
        avg_loss_usd=-4.0, total_fees_usd=1.0, total_funding_usd=0.0,
        exit_reasons={},
    )


def make_result(n=5):
    return WalkForwardResult(windows=tuple(
        WindowResult(i, ts(i * 100), ts(i * 100 + 60), ts(i * 100 + 90),
                     {"p": 1}, metrics(), metrics())
        for i in range(n)
    ))


PROPOSAL = {
    "proposal_id": "variant|binanceusdm|BTC/USDT:USDT|1h|"
                   "trend_continuation_v1|trend_continuation_v1__short_only",
    "proposal_type": "variant_backtest",
    "auto_runnable": True,
    "agent": "bounded_edge_research_agent",
    "exchange": "binanceusdm",
    "symbol": "BTC/USDT:USDT",
    "timeframe": "1h",
    "parent_strategy": "trend_continuation_v1",
    "variant_id": "trend_continuation_v1__short_only",
    "strategy_id": "trend_continuation_v1",
    "fixed_params": {"allowed_sides": "short"},
    "grid_axes": {"breakout_bars": [48, 96]},
    "gates_label": "standard",
    "test_bars": 360,
    "goal": "test short side alone",
    "rationale": "long side drags",
}


class FakeAgent:
    def __init__(self, max_variant_proposals=2):
        pass

    def plan(self, records, targets=()):
        return types.SimpleNamespace(
            proposals=(dict(PROPOSAL),), profitable_pairs=(), policy={},
        )


def candles_frame(n):
    return pd.DataFrame({
        "timestamp": [ts(i) for i in range(n)],
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
        "close": [100.0] * n, "volume": [10.0] * n,
    })


@pytest.fixture
def explore_env(tmp_path, monkeypatch):
    from vnedge.data.parquet_store import ParquetStore

    store = ParquetStore(tmp_path / "data")
    store.upsert_candles("binanceusdm", "BTC/USDT:USDT", "1h", candles_frame(2000))
    monkeypatch.setattr(cr, "OUT_DIR", tmp_path / "live_research")
    monkeypatch.setattr(cr, "EdgeResearchAgent", FakeAgent)
    monkeypatch.setattr(cr, "walk_forward", lambda *a, **k: make_result())
    return store, tmp_path / "burns.jsonl"


def test_auto_explore_same_window_skipped_new_window_allowed(explore_env):
    store, burns = explore_env

    first = cr.auto_explore(store, [], burn_registry_path=burns)
    assert len(first) == 1
    records = data_burn.read_records(burns)
    assert len(records) == 1
    assert records[0]["kind"] == "exploratory_burn"
    assert records[0]["strategy_id"] == "trend_continuation_v1__short_only"
    assert records[0]["verdict"] == first[0]["verdict"]
    state = json.loads((cr.OUT_DIR / "auto_explore.json").read_text())
    assert state["total_attempts"] == 1
    assert len(state["tried"]) == 1
    key = state["tried"][0]
    assert key.startswith(PROPOSAL["proposal_id"] + "|")  # window-fingerprinted

    # Same variant, materially-same window: skipped, no new burn.
    second = cr.auto_explore(store, [], burn_registry_path=burns)
    assert second == []
    assert len(data_burn.read_records(burns)) == 1
    assert json.loads(
        (cr.OUT_DIR / "auto_explore.json").read_text()
    )["total_attempts"] == 1

    # Data rolls 30 days forward: same variant is a genuinely new attempt.
    store.upsert_candles(
        "binanceusdm", "BTC/USDT:USDT", "1h", candles_frame(2000 + 30 * 24)
    )
    third = cr.auto_explore(store, [], burn_registry_path=burns)
    assert len(third) == 1
    assert len(data_burn.read_records(burns)) == 2
    state = json.loads((cr.OUT_DIR / "auto_explore.json").read_text())
    assert state["total_attempts"] == 2
    assert len(state["tried"]) == 2


def test_auto_explore_legacy_keys_still_skipped(explore_env):
    store, burns = explore_env
    cr.OUT_DIR.mkdir(parents=True, exist_ok=True)
    (cr.OUT_DIR / "auto_explore.json").write_text(json.dumps({
        "tried": [PROPOSAL["proposal_id"]],  # old, pre-fingerprint format
        "total_attempts": 1,
    }))
    variants = cr.auto_explore(store, [], burn_registry_path=burns)
    assert variants == []
    assert data_burn.read_records(burns) == []
    state = json.loads((cr.OUT_DIR / "auto_explore.json").read_text())
    assert state["tried"] == [PROPOSAL["proposal_id"]]  # format preserved


def test_auto_explore_burn_failure_does_not_kill_cycle(explore_env, monkeypatch):
    store, _ = explore_env

    def boom(*a, **k):
        raise OSError("registry disk full")

    monkeypatch.setattr(data_burn, "record_burn", boom)
    variants = cr.auto_explore(store, [], burn_registry_path="/nonexistent/x.jsonl")
    assert len(variants) == 1  # exploration result still published
