from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vnedge.research.btc_eth_lead_lag_backtest import (
    PairReplayResult,
    align_closed_pairs,
    selection_gate,
    simulate_pair_window,
)
from vnedge.scalping.delta_engine.lead_lag import (
    BtcEthLeadLagScanner,
    LeadLagContext,
    lead_lag_fee_model,
    load_lead_lag_config,
)
from vnedge.scalping.delta_engine.types import Candle, Side

NOW = datetime(2026, 7, 25, tzinfo=UTC)
CONFIG_PATH = "configs/research/btc_eth_lead_lag_v1.yaml"


def _bar(ts: datetime, close: float, volume: float, *, previous: float) -> Candle:
    opened = previous
    return Candle(
        ts,
        opened,
        max(opened, close) + 0.01,
        min(opened, close) - 0.01,
        close,
        volume,
        "1m",
    )


def _lead_lag_rows() -> tuple[tuple[Candle, ...], tuple[Candle, ...]]:
    btc_closes = [100.0] * 20 + [100.02, 100.06, 100.12, 100.19, 100.25]
    eth_closes = [100.0] * 24 + [100.06]
    btc: list[Candle] = []
    eth: list[Candle] = []
    for index, (btc_close, eth_close) in enumerate(zip(btc_closes, eth_closes, strict=True)):
        ts = NOW - timedelta(minutes=len(btc_closes) - index - 1)
        volume = 300.0 if index == len(btc_closes) - 1 else 90.0 + index % 3 * 10.0
        btc.append(
            _bar(
                ts,
                btc_close,
                volume,
                previous=btc_closes[index - 1] if index else btc_close,
            )
        )
        eth.append(
            _bar(
                ts,
                eth_close,
                volume,
                previous=eth_closes[index - 1] if index else eth_close,
            )
        )
    return tuple(btc), tuple(eth)


def test_preregistered_contract_is_locked_and_research_only():
    config = load_lead_lag_config(CONFIG_PATH)

    assert config.contract_id == "btc_eth_lead_lag_v1"
    assert config.research_only is True
    assert config.can_trade is False
    assert config.can_promote is False
    assert config.validation.open_untouched_only_after_selection_pass is True
    assert config.costs.prefer_maker is False


def test_lead_lag_scanner_emits_one_costed_eth_candidate():
    config = load_lead_lag_config(CONFIG_PATH)
    scanner = BtcEthLeadLagScanner(config, lead_lag_fee_model(config))
    btc, eth = _lead_lag_rows()

    candidate = scanner.evaluate(LeadLagContext(NOW, btc, eth))

    assert candidate is not None
    assert candidate.symbol == "ETHUSD"
    assert candidate.side is Side.LONG
    assert candidate.scanner_id == "btc_eth_lead_lag_v1"
    assert candidate.metadata["leader_symbol"] == "BTCUSD"
    assert candidate.metadata["structural_prior"]["promotion_eligible"] is False
    assert candidate.metadata["l2_confirmation"]["used_for_signal"] is False
    assert candidate.modeled_cost_bps == pytest.approx(14.8)
    assert scanner.evaluate(LeadLagContext(NOW, btc, eth)) is None


def test_lead_lag_scanner_rejects_eth_that_already_followed():
    config = load_lead_lag_config(CONFIG_PATH)
    scanner = BtcEthLeadLagScanner(config, lead_lag_fee_model(config))
    btc, eth = _lead_lag_rows()
    followed = list(eth)
    previous = followed[-2].close
    followed[-1] = _bar(NOW, 100.20, 300.0, previous=previous)

    assert scanner.evaluate(LeadLagContext(NOW, btc, tuple(followed))) is None


def test_pair_alignment_is_exact_and_gap_resets_fail_quality():
    config = load_lead_lag_config(CONFIG_PATH)
    btc, eth = _lead_lag_rows()
    eth_missing = list(eth)
    del eth_missing[-3]

    aligned = align_closed_pairs(list(btc), eth_missing)
    result = simulate_pair_window(aligned, config)

    assert len(aligned) == len(btc) - 1
    assert all(left.ts == right.ts for left, right in aligned)
    assert result.missing_minutes == 1


def test_selection_gate_keeps_untouched_sealed_without_evidence():
    config = load_lead_lag_config(CONFIG_PATH)
    empty = PairReplayResult((), 0, 0, 100, NOW - timedelta(days=2), NOW)

    gate = selection_gate(empty, config)

    assert gate["passed"] is False
    assert gate["checks"]["minimum_trades"] is False
    assert gate["checks"]["average_net"] is False
