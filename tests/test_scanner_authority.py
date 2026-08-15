import json

import yaml

from vnedge.runtime.scanner_authority import build_production_readiness


def test_readiness_can_be_observation_only_when_explicitly_enabled(tmp_path):
    evidence = {
        "experiments": {
            "s1": {
                "contract": {"scanner_id": "s1"},
                "selection": {
                    "metrics": {"trades": 15, "average_net_bps": 5, "profit_factor": 2},
                    "gate": {"passed": False, "checks": {"minimum_trades": False}},
                },
                "untouched": {"status": "sealed", "loaded": False},
            }
        }
    }
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))
    manifest = {
        "venue": "delta_india",
        "markets": ["ETH/USD:USD"],
        "scanner_authority": {
            "scanner_id": "s1",
            "enabled_for_observation": True,
            "enabled_for_paper": False,
            "enabled_for_live": False,
            "selection_evidence": "evidence.json",
            "selection_minimum_trades": 60,
        },
    }
    path = tmp_path / "production.yaml"
    path.write_text(yaml.safe_dump(manifest))

    result = build_production_readiness(path)

    assert result["scanner"]["state"] == "OBSERVATION_ONLY"
    assert not result["scanner"]["selection_passed"]
    assert not result["authority"]["paper_allowed"]
    assert not result["authority"]["live_allowed"]
    assert "selection sample 15/60 trades" in result["blockers"]


def test_failed_selection_defaults_to_blocked_not_fake_observation(tmp_path):
    evidence = {
        "experiments": {
            "s1": {
                "contract": {"scanner_id": "s1"},
                "selection": {
                    "metrics": {"trades": 15},
                    "gate": {"passed": False, "checks": {"minimum_trades": False}},
                },
                "untouched": {"status": "sealed", "loaded": False},
            }
        }
    }
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))
    manifest = {
        "venue": "delta_india",
        "markets": ["ETH/USD:USD"],
        "scanner_authority": {
            "scanner_id": "s1",
            "enabled_for_observation": False,
            "selection_evidence": "evidence.json",
            "selection_minimum_trades": 60,
        },
    }
    path = tmp_path / "production.yaml"
    path.write_text(yaml.safe_dump(manifest))

    result = build_production_readiness(path)

    assert result["scanner"]["state"] == "BLOCKED_BY_SELECTION"
    assert result["ladder"][1]["passed"] is False
