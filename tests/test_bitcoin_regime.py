"""Bitcoin network regime sensor."""

from datetime import UTC, datetime
import json

import pytest

from vnedge.research.bitcoin_regime import (
    BitcoinCoreRpcClient,
    UnsafeBitcoinRpcMethod,
    classify_mempool_stress,
    publish_bitcoin_regime,
    run_bitcoin_regime,
)


def stressed_fixture() -> dict:
    return {
        "node": {
            "chain": "main",
            "synced": True,
            "blocks": 900_000,
            "headers": 900_000,
            "verification_progress": 0.99999,
        },
        "mempool": {
            "tx_count": 240_000,
            "vsize_vb": 420_000_000,
            "min_fee_sat_vb": 7.0,
        },
        "fees": {
            "fastest_fee_sat_vb": 85.0,
            "half_hour_fee_sat_vb": 72.0,
            "hour_fee_sat_vb": 41.0,
        },
    }


def test_bitcoin_regime_classifies_mempool_stress():
    panic = classify_mempool_stress({
        "tx_count": 600_000,
        "vsize_vb": 1_000_000_000,
        "fastest_fee_sat_vb": 180,
    })
    calm = classify_mempool_stress({
        "tx_count": 8_000,
        "vsize_vb": 10_000_000,
        "fastest_fee_sat_vb": 2,
    })

    assert panic["state"] == "panic"
    assert panic["score"] >= 8
    assert calm["state"] == "calm"


def test_bitcoin_regime_fixture_is_research_only_context():
    payload = run_bitcoin_regime(
        fixture=stressed_fixture(),
        now=datetime(2026, 7, 9, tzinfo=UTC),
    )

    assert payload["schema_version"] == "bitcoin_regime_v1"
    assert payload["source"]["status"] == "ok"
    assert payload["summary"]["stress_state"] in {"stressed", "panic"}
    assert payload["policy"]["can_trade"] is False
    assert payload["policy"]["can_promote"] is False
    assert payload["policy"]["wallet_rpc_allowed"] is False
    assert payload["can_trade"] is False
    assert "mempool_pressure_high" in payload["research_tags"]


def test_bitcoin_rpc_client_rejects_non_readonly_methods_before_network():
    client = BitcoinCoreRpcClient("http://127.0.0.1:8332")

    with pytest.raises(UnsafeBitcoinRpcMethod):
        client.call("sendtoaddress", ["address", 1])


def test_bitcoin_regime_history_z_score_and_atomic_publish(tmp_path):
    history = []
    for fee in (10, 11, 12, 13, 14):
        history.append({
            "features": {
                "fastest_fee_sat_vb": fee,
                "mempool_vsize_vb": 100_000_000 + fee,
            }
        })
    payload = run_bitcoin_regime(
        fixture=stressed_fixture(),
        history_records=history,
        now=datetime(2026, 7, 9, tzinfo=UTC),
    )
    out = tmp_path / "bitcoin_regime_latest.json"
    feed = tmp_path / "bitcoin_regime_history.jsonl"

    publish_bitcoin_regime(payload, out, feed)
    publish_bitcoin_regime(payload, out, feed)

    stored = json.loads(out.read_text())
    assert stored["features"]["fee_spike_z"] > 0
    assert not list(tmp_path.glob("*.tmp"))
    assert len(feed.read_text().strip().splitlines()) == 2
