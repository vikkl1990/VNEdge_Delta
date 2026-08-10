"""Immutable fill ledger — hash chain, tamper detection, resume."""

import json

import pytest

from vnedge.execution.fill_ledger import FillLedger, verify_chain


def _fill(i):
    return {"symbol": "BTC/USDT:USDT", "side": "buy", "quantity": 0.01,
            "price": 60_000.0 + i, "fee_usd": 0.3, "mode": "paper"}


def test_chain_appends_and_verifies(tmp_path):
    path = tmp_path / "fills.jsonl"
    ledger = FillLedger(path)
    for i in range(5):
        ledger.append(_fill(i))
    report = verify_chain(path)
    assert report.ok and report.records == 5
    # every record links to its predecessor
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    for prev, cur in zip(lines, lines[1:]):
        assert cur["prev_hash"] == prev["hash"]


def test_tamper_detected(tmp_path):
    path = tmp_path / "fills.jsonl"
    ledger = FillLedger(path)
    for i in range(4):
        ledger.append(_fill(i))
    lines = path.read_text().splitlines()
    doctored = json.loads(lines[1])
    doctored["price"] = 1.0                      # retroactive edit
    lines[1] = json.dumps(doctored, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")

    report = verify_chain(path)
    assert not report.ok
    assert report.first_bad_line == 2
    # and a ledger refuses to extend a broken chain
    with pytest.raises(ValueError, match="chain verification"):
        FillLedger(path)


def test_deletion_detected(tmp_path):
    path = tmp_path / "fills.jsonl"
    ledger = FillLedger(path)
    for i in range(4):
        ledger.append(_fill(i))
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")  # drop record 2
    assert not verify_chain(path).ok


def test_restart_resumes_chain(tmp_path):
    path = tmp_path / "fills.jsonl"
    first = FillLedger(path)
    first.append(_fill(0))
    second = FillLedger(path)                     # simulated restart
    assert second.records == 1
    second.append(_fill(1))
    report = verify_chain(path)
    assert report.ok and report.records == 2


def test_empty_ledger_verifies(tmp_path):
    assert verify_chain(tmp_path / "nope.jsonl").ok


def test_compute_book_metrics():
    from vnedge.exchange.live_feed import compute_book_metrics

    m = compute_book_metrics(
        "BTC/USDT:USDT",
        [[100.0, 2.0], [99.9, 3.0]],
        [[100.1, 1.0], [100.2, 4.0]],
    )
    assert m["spread_bps"] == pytest.approx(10.0, rel=0.01)
    assert -1.0 <= m["imbalance"] <= 1.0
    assert m["liq_usd_5bps"] > 0
    # crossed book -> None (keep last good metrics)
    assert compute_book_metrics("X", [[101.0, 1.0]], [[100.0, 1.0]]) is None
