import subprocess

from vnedge.runtime import event_readiness_publisher as publisher


def test_code_version_marks_untracked_tree_dirty(monkeypatch, tmp_path):
    monkeypatch.delenv("VNEDGE_BUILD_SHA", raising=False)
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: "abc\n")

    assert publisher.code_version(tmp_path) == "abc+dirty"


def test_publisher_keeps_scanner_and_orders_locked(monkeypatch, tmp_path):
    monkeypatch.setattr(
        publisher,
        "qualify_event_continuity",
        lambda *args, **kwargs: {
            "qualification": {
                "data_ready": True,
                "qualified_events": 5_000_000,
                "target_events": 5_000_000,
                "qualified_days": 14,
                "target_days": 14,
                "blockers": [],
            }
        },
    )
    monkeypatch.setattr(
        publisher,
        "publish_production_readiness",
        lambda *args, **kwargs: {"scanner": {"state": "BLOCKED_BY_SELECTION"}},
    )

    result = publisher.publish_once(
        event_root=tmp_path / "events",
        contract=tmp_path / "contract.yaml",
        continuity_output=tmp_path / "continuity.json",
        continuity_cache=tmp_path / "cache.json",
        production_manifest=tmp_path / "production.yaml",
        production_output=tmp_path / "production.json",
        status_output=tmp_path / "status.json",
        version="abc",
    )

    assert result["status"] == "READY"
    assert result["scanner_implementation_authorized"] is False
    assert result["selection_authorized"] is False
    assert result["order_route"] == "absent"
    assert result["can_trade"] is False
    assert result["can_promote"] is False
