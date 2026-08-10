from __future__ import annotations

from datetime import UTC, datetime

from vnedge.agent_gateway.task_registry import QuantOSAgentGateway
from vnedge.research.quantified_port_factory import (
    QUANTIFIED_PORT_FACTORY_ID,
    QuantifiedPortFactoryConfig,
    build_quantified_port_factory_payload,
    load_quantified_port_factory_payload,
    publish_quantified_port_factory,
)
from vnedge.research.quantified_strategy_lab import build_quantified_strategy_lab_payload


def _lab() -> dict:
    return build_quantified_strategy_lab_payload(
        generated_at=datetime(2026, 7, 30, tzinfo=UTC)
    )


def test_port_factory_builds_chunk_a_first_without_trade_permission() -> None:
    payload = build_quantified_port_factory_payload(
        lab_payload=_lab(),
        config=QuantifiedPortFactoryConfig(sync_gateway=False),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    assert payload["factory_id"] == QUANTIFIED_PORT_FACTORY_ID
    assert payload["summary"]["blueprints"] == 6
    assert payload["summary"]["chunks"]["A"] == 3
    assert payload["blueprints"][0]["chunk"] == "A"
    assert payload["blueprints"][0]["recommended_port"] in {
        "bitcoin_crypto_strategy_pack_v1",
        "range_volatility_breakout_reversion_v1",
        "pullback_reversion_pack_v1",
    }
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False
    assert all(row["live_orders_enabled"] is False for row in payload["blueprints"])


def test_port_factory_declares_execution_realistic_replay_contract() -> None:
    payload = build_quantified_port_factory_payload(
        lab_payload=_lab(),
        config=QuantifiedPortFactoryConfig(chunks=("A",), sync_gateway=False),
    )

    by_port = {row["recommended_port"]: row for row in payload["blueprints"]}
    breakout = by_port["range_volatility_breakout_reversion_v1"]

    assert breakout["execution_replay"]["maker_first"] is True
    assert "tp1_partial_be_then_trail" in breakout["execution_replay"]["compare_exits"]
    assert breakout["promotion_gate"]["min_net_bps"] == 25.0
    assert breakout["promotion_gate"]["min_profit_factor"] == 1.5
    assert breakout["promotion_gate"]["min_trades"] == 20
    assert "5m" in breakout["replay_matrix"]["timeframes"]
    assert "delta_india" in breakout["replay_matrix"]["venues"]


def test_port_factory_syncs_quant_os_tasks_once(tmp_path) -> None:
    lab = _lab()
    config = QuantifiedPortFactoryConfig(chunks=("A",), sync_gateway=True)

    first = build_quantified_port_factory_payload(
        lab_payload=lab,
        config=config,
        gateway_dir=tmp_path / "quant_os",
    )
    second = build_quantified_port_factory_payload(
        lab_payload=lab,
        config=config,
        gateway_dir=tmp_path / "quant_os",
    )
    snapshot = QuantOSAgentGateway(tmp_path / "quant_os").snapshot(limit=20)

    assert first["summary"]["gateway_tasks_created"] == 3
    assert first["summary"]["gateway_artifacts_registered"] == 3
    assert second["summary"]["gateway_tasks_created"] == 0
    assert second["summary"]["gateway_tasks_reused"] == 3
    assert snapshot["summary"]["total_tasks"] == 3
    assert all(task["can_trade"] is False for task in snapshot["tasks"])


def test_publish_and_load_port_factory_round_trip(tmp_path) -> None:
    payload = build_quantified_port_factory_payload(
        lab_payload=_lab(),
        config=QuantifiedPortFactoryConfig(chunks=("A",), sync_gateway=False),
    )
    out = tmp_path / "quantified_port_factory_latest.json"
    feed = tmp_path / "quantified_port_factory_feed.jsonl"

    publish_quantified_port_factory(payload, out=out, feed=feed)
    loaded = load_quantified_port_factory_payload(out)

    assert loaded["factory_id"] == QUANTIFIED_PORT_FACTORY_ID
    assert loaded["summary"]["chunks"]["A"] == 3
    assert feed.exists()
    assert QUANTIFIED_PORT_FACTORY_ID in feed.read_text(encoding="utf-8")
