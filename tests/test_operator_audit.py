"""Operator-action audit log — durable, append-only, hash-chained, tamper-evident."""

import json

import pytest

from vnedge.execution.operator_audit import OperatorAuditLog
from vnedge.risk.kill_switch import KillSwitch


def test_records_are_hash_chained_and_verify(tmp_path):
    log = OperatorAuditLog(tmp_path / "audit.jsonl")
    h1 = log.record(actor="operator", action="live_gate_flip", detail="enabled", after="live_small")
    h2 = log.record(actor="operator", action="strategy_promote", detail="funding_mr")
    assert h1 != h2
    rows = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["hash"] == h1 and rows[1]["prev_hash"] == h1   # chained
    assert log.verify_chain().ok


def test_tampering_breaks_the_chain(tmp_path):
    p = tmp_path / "audit.jsonl"
    log = OperatorAuditLog(p)
    log.record(actor="operator", action="a"); log.record(actor="operator", action="b")
    rows = p.read_text().splitlines()
    edited = json.loads(rows[0]); edited["detail"] = "TAMPERED"
    p.write_text("\n".join([json.dumps(edited), rows[1]]) + "\n")
    report = log.verify_chain()
    assert not report.ok and report.first_bad_line == 1


def test_resume_continues_chain_and_refuses_broken(tmp_path):
    p = tmp_path / "audit.jsonl"
    OperatorAuditLog(p).record(actor="operator", action="first")
    # reopening continues the chain cleanly
    reopened = OperatorAuditLog(p)
    reopened.record(actor="operator", action="second")
    assert reopened.verify_chain().ok and reopened.verify_chain().lines == 2
    # a corrupted tail is refused on open
    p.write_text(p.read_text() + '{"actor":"x","action":"forged","hash":"deadbeef","prev_hash":"nope"}\n')
    with pytest.raises(RuntimeError):
        OperatorAuditLog(p)


def test_kill_switch_persists_trip_and_reset_to_the_audit_log(tmp_path):
    audit = OperatorAuditLog(tmp_path / "audit.jsonl")
    ks = KillSwitch(kill_file=tmp_path / "KILL", audit_log=audit)
    ks.activate("daily loss breached", source="programmatic")
    ks.reset("investigated + cleared")
    actions = [json.loads(l)["action"] for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert actions == ["kill_switch_activate", "kill_switch_reset"]
    assert audit.verify_chain().ok


def test_kill_switch_without_audit_log_is_unchanged(tmp_path):
    # backward-compat: no audit_log wired -> behaves exactly as before, no crash
    ks = KillSwitch(kill_file=tmp_path / "KILL")
    ks.activate("test")
    assert ks.is_active
