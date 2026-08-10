"""Pre-live readiness status — the gates to a first live order, made visible.

``run_pre_live_checklist`` is runtime-only. This wraps it into a cheap,
on-demand status projection for the dashboard so the operator can SEE exactly
what stands between the current (paper) system and a first ``live_small`` order,
and — critically — WHO must act on each red gate:

- ``deliberate``  — off by design (the three live gates); flip only at the end.
- ``operator``    — a human action (install mainnet trade-only keys).
- ``system``      — earned by the system (evidence, attestation, clean recon).

Read-only. Computes booleans only; it never reads or emits secret values, and
it cannot enable live trading.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

# who is responsible for turning each gate green
_OWNER = {
    "three_live_gates": "deliberate",
    "kill_switch_clear": "system",
    "trade_credentials_present": "operator",
    "reconciliation_clean": "system",
    "journal_writable": "system",
    "mode_ladder_validated": "system",
    "private_stream_connected": "system",
    "private_stream_fresh": "system",
}

# the ordered path a human/operator walks to a first live_small order
_PATH_TO_LIVE = [
    {"step": "Promote a strategy through untouched-data judgment → shadow → paper",
     "owner": "system+human", "detail": "needs a lane that clears the promotion gates "
     "(currently 0 paper-review-ready — blocked on shadow-trade evidence)"},
    {"step": "Install mainnet trade-only API keys",
     "owner": "operator", "detail": "env VNEDGE_EXEC_API_KEY / VNEDGE_EXEC_API_SECRET "
     "(trade-only, no withdrawal) — never pasted in chat"},
    {"step": "Run the live_ladder attestation",
     "owner": "system", "detail": "each lower rung (paper/shadow/live_small) validated "
     "against its locked thresholds"},
    {"step": "Execution drill at minimum size on mainnet",
     "owner": "system", "detail": "one real order round-trip through the full pipeline"},
    {"step": "Flip the three live gates for live_small (capped capital)",
     "owner": "operator", "detail": "live_* mode + live_trading_enabled=true + "
     "confirm_live_trading=I_UNDERSTAND_THIS_IS_HIGH_RISK"},
]


def _credentials_present(environ: Mapping[str, str]) -> bool:
    # presence only — the actual secret is never read into the payload
    return bool(environ.get("VNEDGE_EXEC_API_KEY") and environ.get("VNEDGE_EXEC_API_SECRET"))


def _ladder_validated(ladder_path: Path | None) -> bool:
    if ladder_path is None or not ladder_path.exists():
        return False
    try:
        import json

        payload = json.loads(ladder_path.read_text())
    except (OSError, ValueError):
        return False
    return bool(payload.get("all_rungs_validated") or payload.get("cleared"))


def build_pre_live_status(
    *,
    journal_dir: Path,
    environ: Mapping[str, str] = os.environ,
    ladder_path: Path | None = None,
    kill_file: Path | None = None,
) -> dict:
    """Assemble the pre-live readiness projection. Never raises for a caller —
    on any error it returns an honest 'unavailable' payload."""
    try:
        from vnedge.config.risk_config import RiskConfig
        from vnedge.config.settings import Settings
        from vnedge.runtime.pre_live_checklist import run_pre_live_checklist

        settings = Settings()
        report = run_pre_live_checklist(
            settings=settings,
            risk_config=RiskConfig(),
            kill_switch_active=(kill_file or Path("KILL")).exists(),
            has_unresolved_orders=False,  # status context: reconciliation runs in the trader
            journal_path=Path(journal_dir) / "pre_live_probe.journal.jsonl",
            credentials_present=_credentials_present(environ),
            lower_rungs_validated=_ladder_validated(ladder_path),
        )
        checks = []
        for c in report.to_dict()["checks"]:
            checks.append({**c, "owner": _OWNER.get(c["name"], "system")})
        reds = [c for c in checks if not c["passed"]]
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "cleared": all(c["passed"] for c in checks if c["critical"]),
            "checks": checks,
            "red_count": len(reds),
            "operator_action_reds": [c["name"] for c in reds if c["owner"] == "operator"],
            "path_to_live": _PATH_TO_LIVE,
            "operator_answer": (
                "all critical live gates green — ready for the execution drill"
                if all(c["passed"] for c in checks if c["critical"])
                else f"{len(reds)} gate(s) still red — see the path to live"
            ),
            "can_trade": False,
            "can_promote": False,
            "live_orders_enabled": False,
        }
    except Exception as exc:  # noqa: BLE001 — a status surface must never crash
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "cleared": False,
            "checks": [],
            "path_to_live": _PATH_TO_LIVE,
            "operator_answer": f"pre-live checklist unavailable: {str(exc)[:160]}",
            "can_trade": False,
            "can_promote": False,
        }
