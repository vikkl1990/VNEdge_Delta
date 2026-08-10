"""MLflow-style experiment recorder — log params/metrics/artifacts, then search."""

import pytest

from vnedge.research.experiment_recorder import (
    STATUS_FAILED,
    STATUS_FINISHED,
    STATUS_RUNNING,
    ExperimentRecorder,
)


def test_log_and_get_run(tmp_path):
    rec = ExperimentRecorder(tmp_path)
    rid = rec.start_run("wf-funding-mr", tags={"symbol": "BTC"})
    rec.log_params(rid, {"z_entry": 1.5, "extreme_pct": 0.85})
    rec.log_metrics(rid, {"profit_factor": 1.4, "oos_net_usd": 16.0})
    rec.log_metrics(rid, {"profit_factor": 1.5})  # latest wins
    rec.set_status(rid, STATUS_FINISHED)
    run = rec.get_run(rid)
    assert run["params"]["z_entry"] == 1.5
    assert run["metrics"]["profit_factor"] == 1.5  # overwritten
    assert run["tags"]["symbol"] == "BTC" and run["status"] == STATUS_FINISHED


def test_artifacts_are_saved_and_listed(tmp_path):
    rec = ExperimentRecorder(tmp_path)
    rid = rec.start_run("bt")
    p = rec.log_artifact(rid, "equity_curve.json", '{"pts":[1,2,3]}')
    assert p.exists() and p.read_text() == '{"pts":[1,2,3]}'
    assert rec.get_run(rid)["artifacts"] == ["equity_curve.json"]


def test_search_records_by_name_tag_status_and_predicate(tmp_path):
    rec = ExperimentRecorder(tmp_path)
    a = rec.start_run("wf", tags={"symbol": "BTC"}); rec.log_metrics(a, {"pf": 1.4}); rec.set_status(a, STATUS_FINISHED)
    b = rec.start_run("wf", tags={"symbol": "ETH"}); rec.log_metrics(b, {"pf": 0.9}); rec.set_status(b, STATUS_FINISHED)
    c = rec.start_run("scan", tags={"symbol": "BTC"})  # still RUNNING

    assert {r["run_id"] for r in rec.search_records(name="wf")} == {a, b}
    assert [r["run_id"] for r in rec.search_records(tag=("symbol", "ETH"))] == [b]
    assert [r["run_id"] for r in rec.search_records(status=STATUS_RUNNING)] == [c]
    winners = rec.search_records(where=lambda r: r["metrics"].get("pf", 0) > 1.2)
    assert [r["run_id"] for r in winners] == [a]


def test_run_context_marks_finished_and_failed(tmp_path):
    rec = ExperimentRecorder(tmp_path)
    with rec.run("ok") as run:
        run.log_params({"x": 1})
        ok_id = run.run_id
    assert rec.get_run(ok_id)["status"] == STATUS_FINISHED

    with pytest.raises(ValueError):
        with rec.run("boom") as run:
            bad_id = run.run_id
            raise ValueError("kaboom")
    assert rec.get_run(bad_id)["status"] == STATUS_FAILED


def test_unknown_run_and_empty_store(tmp_path):
    rec = ExperimentRecorder(tmp_path / "empty")
    assert rec.runs() == []
    with pytest.raises(KeyError):
        rec.log_metrics("run_nope", {"x": 1})
