import json

from vnedge.dashboard.session_regime import (
    build_session_regime,
    _breakeven_cushion,
    _session_key,
    _worst_stretch,
)
from datetime import UTC, datetime


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def _outcome(ts, net, side="long", res="stop"):
    return {
        "ts": ts,
        "kind": "shadow_outcome",
        "payload": {
            "symbol": "BTC/USDT:USDT",
            "side": side,
            "resolution": res,
            "entry_price": 100.0,
            "exit_price": 100.0 + net,
            "virtual_net_usd": net,
            "fees_usd": 0.0,
            "intent_key": f"k{ts}",
            "bars_held": 0,  # entry == exit, so the ts hour is the session
        },
    }


def test_worst_stretch_is_peak_to_trough():
    # +5, -3, -4, +2  -> cumulative 5,2,-2,0 ; peak 5 ; trough at -2 -> -7
    assert _worst_stretch([5, -3, -4, 2]) == -7
    assert _worst_stretch([1, 2, 3]) == 0  # never dips below peak
    assert _worst_stretch([]) == 0


def test_session_key_bands():
    assert _session_key(datetime(2026, 8, 1, 3, tzinfo=UTC)) == "asia"
    assert _session_key(datetime(2026, 8, 1, 10, tzinfo=UTC)) == "europe"
    assert _session_key(datetime(2026, 8, 1, 15, tzinfo=UTC)) == "us"
    assert _session_key(datetime(2026, 8, 1, 22, tzinfo=UTC)) == "late"


def test_breakeven_cushion_positive_when_edge_beats_payoff():
    # 2 wins of +10, 2 losses of -1: avg_win 10, avg_loss 1, be_win=1/11≈9.1%,
    # actual win 50% -> cushion ≈ +40.9
    cushion = _breakeven_cushion([10, 10], [-1, -1])
    assert cushion is not None and cushion > 40


def test_build_session_regime_buckets_by_entry_session(tmp_path):
    write_jsonl(
        tmp_path / "mystrat_binanceusdm_btcusdt_1h_shadow.journal.jsonl",
        [
            _outcome("2026-08-01T03:00:00+00:00", 5.0),   # asia, win
            _outcome("2026-08-01T15:00:00+00:00", -3.0),  # us, loss
            _outcome("2026-08-01T16:00:00+00:00", -2.0),  # us, loss
        ],
    )
    snap = {
        "lanes": [{"lane_id": "mystrat_binanceusdm_btcusdt_1h_shadow"}],
        "lane_id": "mystrat_binanceusdm_btcusdt_1h_shadow",
    }
    out = build_session_regime(snapshot=snap, journal_dir=tmp_path)

    by = {s["session"]: s for s in out["by_session"]}
    assert by["asia"]["trades"] == 1
    assert by["asia"]["net_usd"] == 5.0
    assert by["asia"]["win_rate_pct"] == 100.0
    assert by["us"]["trades"] == 2
    assert by["us"]["net_usd"] == -5.0
    assert by["us"]["win_rate_pct"] == 0.0
    assert by["us"]["worst_stretch_usd"] == -5.0
    assert by["europe"]["trades"] == 0

    # matrix rolls up under the strategy prefix (before the exchange token)
    strat = {r["strategy"]: r for r in out["matrix"]}
    assert "mystrat" in strat
    assert strat["mystrat"]["total"]["trades"] == 3
    assert strat["mystrat"]["sessions"]["us"]["net_usd"] == -5.0

    assert out["overall"]["trades"] == 3
    assert out["best_session"]["session"] == "asia"
    assert out["worst_session"]["session"] == "us"


def test_retired_lane_excluded_from_fleet_view(tmp_path):
    # A journal on disk whose lane is NOT in the live snapshot must not leak.
    write_jsonl(
        tmp_path / "deadstrat_bybit_ethusdt_1h_shadow.journal.jsonl",
        [_outcome("2026-08-01T03:00:00+00:00", -50.0)],
    )
    snap = {"lanes": [{"lane_id": "livestrat_binanceusdm_btcusdt_1h_shadow"}]}
    out = build_session_regime(snapshot=snap, journal_dir=tmp_path)
    assert out["overall"]["trades"] == 0  # dead lane filtered out
