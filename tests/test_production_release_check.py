from pathlib import Path

from vnedge.runtime.production_release_check import validate_lock, validate_runtime_config


def test_production_lock_is_exact_and_complete():
    count, blockers = validate_lock(Path("requirements-production.lock"))
    assert blockers == []
    assert count >= 40


def test_production_manifest_defaults_fail_closed():
    assert validate_runtime_config(Path("configs/production_live.yaml")) == []


def test_release_lock_rejects_ranges_and_missing_packages(tmp_path):
    lock = tmp_path / "bad.lock"
    lock.write_text("fastapi>=0.1\n")
    _, blockers = validate_lock(lock)
    assert any("unlocked requirement" in blocker for blocker in blockers)
    assert any("not locked" in blocker for blocker in blockers)
