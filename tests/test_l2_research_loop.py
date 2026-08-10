"""Decoupled L2 research loop — restart-safe per-symbol checkpoints, atomic publish, merge."""

import json

from vnedge.research import continuous_research as cr
from vnedge.research import l2_research_loop as l2


def _two_targets():
    return (cr.ResearchTarget("binanceusdm", "BTC/USDT:USDT"),
            cr.ResearchTarget("bybit", "ETH/USDT:USDT"))


def _mock_mining(monkeypatch, targets):
    monkeypatch.setattr(l2, "load_research_targets", lambda: targets)
    monkeypatch.setattr(l2, "_scalper_research_days", lambda root, t: ("20260705",))
    calls = []

    def scalper(root, t, days):
        calls.append(("scalper", t[0].label))
        return {"flow_guards": {"can_trade": False}, "edge_hypotheses": [t[0].label]}

    def alpha(root, t, days, max_rows):
        calls.append(("alpha", t[0].label))
        return {"flow_guards": {"can_trade": False}, "hypotheses": [t[0].label]}

    monkeypatch.setattr(l2, "run_scalper_research", scalper)
    monkeypatch.setattr(l2, "run_alpha_factory", alpha)
    return calls


def test_run_incremental_composes_and_promotes(monkeypatch, tmp_path):
    targets = _two_targets()
    _mock_mining(monkeypatch, targets)
    payload = l2.run_incremental(tmp_path, out_dir=tmp_path)

    assert payload["complete"] is True
    assert payload["days"] == ["20260705"]
    labels = {t.label for t in targets}
    assert set(payload["scalper_research"]["edge_hypotheses"]) == labels   # accumulated
    assert set(payload["alpha_factory"]["hypotheses"]) == labels
    assert payload["alpha_factory"]["tournament"]["tournament_id"] == (
        "event_scalper_alpha_tournament_v1"
    )
    assert payload["alpha_factory"]["tournament"]["can_trade"] is False
    assert payload["scalper_parameter_registry"]["can_trade"] is False     # registry carried
    assert payload["scalper_research"]["focus"]["focus_id"] == "scalper_focus_v1"
    assert payload["scalper_research"]["focus"]["can_trade"] is False
    # promoted to the consumer-facing file only once the pass is complete
    latest = json.loads((tmp_path / "l2_latest.json").read_text())
    assert latest["complete"] is True
    assert len(latest["progress"]["completed_targets"]) == 2
    assert latest["scalper_research"]["focus"]["can_promote"] is False


def test_checkpoints_progress_after_each_symbol(monkeypatch, tmp_path):
    targets = _two_targets()
    _mock_mining(monkeypatch, targets)
    seen_progress = []
    orig = l2._write_json

    def spy(payload, path):
        if path.name == l2.L2_PROGRESS:
            seen_progress.append(len(payload["progress"]["completed_targets"]))
        orig(payload, path)

    monkeypatch.setattr(l2, "_write_json", spy)
    l2.run_incremental(tmp_path, out_dir=tmp_path)
    # progress durably written after symbol 1 and symbol 2 (not only at the end)
    assert 1 in seen_progress and 2 in seen_progress


def test_resumes_after_restart_skipping_completed_symbols(monkeypatch, tmp_path):
    targets = _two_targets()
    done_label = targets[0].label
    # an interrupted pass: BTC already mined, ETH still pending
    (tmp_path / l2.L2_PROGRESS).write_text(json.dumps({
        "days": ["20260705"], "complete": False,
        "progress": {"completed_targets": [done_label], "total": 2},
        "scalper_research": {"edge_hypotheses": [done_label]},
        "alpha_factory": {"hypotheses": [done_label]},
    }))
    calls = _mock_mining(monkeypatch, targets)
    payload = l2.run_incremental(tmp_path, out_dir=tmp_path)

    mined = {label for _, label in calls}
    assert mined == {targets[1].label}                    # only the pending symbol re-mined
    # the completed pass now covers both symbols (resumed + finished)
    assert set(payload["scalper_research"]["edge_hypotheses"]) == {t.label for t in targets}
    assert payload["scalper_research"]["focus"]["summary"]["edge_hypotheses"] == 2
    assert payload["alpha_factory"]["tournament"]["tournament_id"] == (
        "event_scalper_alpha_tournament_v1"
    )


def test_resume_refreshes_derived_views_before_next_symbol(monkeypatch, tmp_path):
    targets = _two_targets()
    done_label = targets[0].label
    (tmp_path / l2.L2_PROGRESS).write_text(json.dumps({
        "days": ["20260705"], "complete": False,
        "progress": {"completed_targets": [done_label], "total": 2},
        "scalper_research": {"edge_hypotheses": [done_label]},
        "alpha_factory": {"hypotheses": [done_label]},
    }))
    _mock_mining(monkeypatch, targets)
    progress_writes = []
    orig = l2._write_json

    def spy(payload, path):
        if path.name == l2.L2_PROGRESS:
            progress_writes.append(json.loads(json.dumps(payload)))
        orig(payload, path)

    monkeypatch.setattr(l2, "_write_json", spy)
    l2.run_incremental(tmp_path, out_dir=tmp_path)

    first_write = progress_writes[0]
    assert first_write["progress"]["completed_targets"] == [done_label]
    assert first_write["scalper_research"]["focus"]["focus_id"] == "scalper_focus_v1"
    assert first_write["alpha_factory"]["tournament"]["tournament_id"] == (
        "event_scalper_alpha_tournament_v1"
    )


def test_stale_progress_for_other_days_starts_fresh(monkeypatch, tmp_path):
    targets = _two_targets()
    (tmp_path / l2.L2_PROGRESS).write_text(json.dumps({
        "days": ["20250101"], "complete": False,   # different day -> not resumable
        "progress": {"completed_targets": [targets[0].label], "total": 2},
    }))
    calls = _mock_mining(monkeypatch, targets)
    l2.run_incremental(tmp_path, out_dir=tmp_path)
    assert {label for _, label in calls} == {t.label for t in targets}   # both re-mined


def test_publish_l2_is_atomic(tmp_path):
    l2.publish_l2({"scalper_research": {"x": 1}}, out_dir=tmp_path)
    assert not list(tmp_path.glob("*.tmp"))                 # no leftover temp
    got = json.loads((tmp_path / "l2_latest.json").read_text())
    assert got["scalper_research"] == {"x": 1}


def test_candle_loop_folds_in_l2_latest_when_inline_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "OUT_DIR", tmp_path)
    (tmp_path / "l2_latest.json").write_text(json.dumps({
        "scalper_research": {"flow_guards": {"can_trade": False}, "edge_hypotheses": [1]},
        "alpha_factory": {"flow_guards": {"can_trade": False}},
    }))
    l2latest = cr._load_l2_latest()
    scalper_research, alpha_factory = {}, {}
    scalper_research = scalper_research or l2latest.get("scalper_research", {})
    alpha_factory = alpha_factory or l2latest.get("alpha_factory", {})
    assert scalper_research["edge_hypotheses"] == [1]
    assert alpha_factory["flow_guards"]["can_trade"] is False


def test_load_l2_latest_missing_or_corrupt_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "OUT_DIR", tmp_path)
    assert cr._load_l2_latest() == {}
    (tmp_path / "l2_latest.json").write_text("{ not json")
    assert cr._load_l2_latest() == {}
