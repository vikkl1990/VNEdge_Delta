from __future__ import annotations

import numpy as np

from vnedge.research.btc_eth_transfer_entropy import (
    DiscreteWindow,
    load_transfer_entropy_config,
    transfer_entropy_bits,
    transfer_entropy_test,
)

CONFIG = "configs/research/btc_eth_transfer_entropy_v1.yaml"


def test_transfer_entropy_contract_is_selection_only_and_non_trading():
    config = load_transfer_entropy_config(CONFIG)

    assert config.research_only is True
    assert config.can_trade is False
    assert config.can_promote is False
    assert config.data.end < config.data.sealed_tail_start
    assert config.data.sealed_tail_access_forbidden is True
    assert config.advancement.scanner_backtest_authorized is False
    assert config.advancement.old_v1_tail_may_open is False


def test_transfer_entropy_detects_non_linear_directed_information():
    rng = np.random.default_rng(7)
    count = 20_000
    source = rng.integers(0, 3, count, dtype=np.int8)
    target = rng.integers(0, 3, count, dtype=np.int8)
    target[1:] = (source[:-1] * source[:-1]) % 3
    segment = np.zeros(count, dtype=np.int64)

    forward, _, _ = transfer_entropy_bits(source, target, segment, history=1)
    reverse, _, _ = transfer_entropy_bits(target, source, segment, history=1)

    assert forward > 0.20
    assert forward > reverse * 10


def test_surrogate_test_is_deterministic_and_significant_for_synthetic_flow():
    rng = np.random.default_rng(19)
    count = 12_000
    source = rng.integers(0, 3, count, dtype=np.int8)
    target = rng.integers(0, 3, count, dtype=np.int8)
    target[1:] = source[:-1]
    window = DiscreteWindow(
        timestamps=np.arange(count),
        btc=source,
        eth=target,
        segment=np.zeros(count, dtype=np.int64),
    )

    first = transfer_entropy_test(window, "btc_to_eth", 1, 39, 42, 20)
    second = transfer_entropy_test(window, "btc_to_eth", 1, 39, 42, 20)

    assert first == second
    assert first["empirical_p_value"] == 0.025
    assert float(first["effective_te_bits"]) > 1.0


def test_history_never_crosses_gap_segments():
    source = np.array([0, 1, 2, 0, 1, 2], dtype=np.int8)
    target = np.array([1, 2, 0, 1, 2, 0], dtype=np.int8)
    segment = np.array([0, 0, 0, 1, 1, 1], dtype=np.int64)

    _, _, observations = transfer_entropy_bits(source, target, segment, history=2)

    assert observations == 2
