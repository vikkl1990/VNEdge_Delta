"""Pre-live readiness status: the gates to a first live order, made visible.

Must be honest (reds are red), never leak secrets (booleans only), never enable
trading, and never crash a status caller."""

from __future__ import annotations

import json

from vnedge.research.pre_live_status import build_pre_live_status


def test_reports_honest_reds_and_owners(tmp_path):
    d = build_pre_live_status(journal_dir=tmp_path, environ={})
    json.dumps(d, allow_nan=False)
    assert d["cleared"] is False
    assert d["can_trade"] is False and d["can_promote"] is False
    names = {c["name"]: c for c in d["checks"]}
    # the three RED gates from the readiness audit
    assert names["three_live_gates"]["passed"] is False
    assert names["three_live_gates"]["owner"] == "deliberate"
    assert names["trade_credentials_present"]["passed"] is False
    assert names["trade_credentials_present"]["owner"] == "operator"
    assert names["mode_ladder_validated"]["passed"] is False
    # credentials show up in the operator action list
    assert "trade_credentials_present" in d["operator_action_reds"]
    # the ordered path to live is present and the last step is flipping the gates
    assert len(d["path_to_live"]) >= 4
    assert "gates" in d["path_to_live"][-1]["step"].lower()


def test_credentials_check_is_presence_only_never_the_value(tmp_path):
    # a present (dummy) credential flips only the boolean — the value is never
    # copied into the payload
    env = {"VNEDGE_EXEC_API_KEY": "k-secret", "VNEDGE_EXEC_API_SECRET": "s-secret"}
    d = build_pre_live_status(journal_dir=tmp_path, environ=env)
    blob = json.dumps(d)
    assert "k-secret" not in blob and "s-secret" not in blob
    cred = next(c for c in d["checks"] if c["name"] == "trade_credentials_present")
    assert cred["passed"] is True


def test_kill_switch_tripped_shows_red(tmp_path):
    kill = tmp_path / "KILL"
    kill.write_text("tripped")
    d = build_pre_live_status(journal_dir=tmp_path, environ={}, kill_file=kill)
    kc = next(c for c in d["checks"] if c["name"] == "kill_switch_clear")
    assert kc["passed"] is False
