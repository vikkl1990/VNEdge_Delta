"""Pre-live checklist — the fail-closed pre-flight before any live order path.

Phase 1 goal: prove the execution engine can go live safely. Before a
LiveTraderSession runs against a real venue, EVERY critical check here must be
green; any red blocks go-live. Explainable by construction — each check carries
its own pass/fail detail, and the report lists every failure, not just the
first (same principle as the risk gateway).

    python -m vnedge.runtime.pre_live_checklist

This is a pre-flight gate, not a bypass: it never enables anything. Live orders
still require the three settings gates AND the adapter's mainnet confirmation.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from vnedge.config.risk_config import ABSOLUTE_MAX_LEVERAGE, RiskConfig
from vnedge.config.settings import LIVE_CONFIRMATION_PHRASE, Settings, TradingMode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    critical: bool = True


@dataclass(frozen=True)
class ChecklistReport:
    results: tuple[CheckResult, ...]

    @property
    def cleared(self) -> bool:
        """True only if every CRITICAL check passed."""
        return all(r.passed for r in self.results if r.critical)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    def to_dict(self) -> dict:
        return {
            "cleared": self.cleared,
            "checks": [
                {"name": r.name, "passed": r.passed,
                 "critical": r.critical, "detail": r.detail}
                for r in self.results
            ],
        }


def run_pre_live_checklist(
    *,
    settings: Settings,
    risk_config: RiskConfig,
    kill_switch_active: bool,
    has_unresolved_orders: bool,
    journal_path: Path,
    credentials_present: bool,
    lower_rungs_validated: bool,
    private_stream_required: bool = False,
    private_stream_connected: bool | None = None,
    private_stream_age_seconds: float | None = None,
    max_private_stream_age_seconds: float = 5.0,
) -> ChecklistReport:
    """Evaluate every live precondition. Fail-closed: any critical red blocks."""
    r: list[CheckResult] = []

    gates_ok = settings.is_live
    r.append(CheckResult(
        "three_live_gates", gates_ok,
        f"mode={settings.trading_mode.value}, live_trading_enabled="
        f"{settings.live_trading_enabled}, phrase_ok="
        f"{settings.confirm_live_trading == LIVE_CONFIRMATION_PHRASE}"
        + ("" if gates_ok else " — all three are required"),
    ))

    r.append(CheckResult(
        "kill_switch_clear", not kill_switch_active,
        "kill switch not tripped" if not kill_switch_active
        else "KILL is active — clear it explicitly before live",
    ))

    r.append(CheckResult(
        "trade_credentials_present", credentials_present,
        "trade-only execution credentials present" if credentials_present
        else "no execution credentials — set trade-only keys via env on the VM",
    ))

    frozen = bool(getattr(type(risk_config), "model_config", {}).get("frozen", False))
    lev = int(getattr(risk_config, "max_leverage_per_position", 0) or 0)
    lev_ok = 0 < lev <= ABSOLUTE_MAX_LEVERAGE
    risk_ok = (
        frozen
        and risk_config.max_daily_loss_usd > 0
        and risk_config.min_account_equity_usd > 0
        and lev_ok
    )
    r.append(CheckResult(
        "risk_config_frozen_valid", risk_ok,
        f"frozen={frozen}, daily_loss=${risk_config.max_daily_loss_usd}, "
        f"min_equity=${risk_config.min_account_equity_usd}, "
        f"leverage={lev}/{ABSOLUTE_MAX_LEVERAGE}",
    ))

    r.append(CheckResult(
        "reconciliation_clean", not has_unresolved_orders,
        "no unresolved orders" if not has_unresolved_orders
        else "unresolved (timeout-unknown) orders present — reconcile to venue "
             "truth before live",
    ))

    if private_stream_required:
        connected = bool(private_stream_connected)
        age = (
            float("inf")
            if private_stream_age_seconds is None
            else float(private_stream_age_seconds)
        )
        stream_ok = connected and age <= max_private_stream_age_seconds
        r.append(CheckResult(
            "private_stream_fresh",
            stream_ok,
            (
                f"private order/fill stream connected age={age:.2f}s "
                f"(max {max_private_stream_age_seconds:.2f}s)"
                if stream_ok else
                f"private order/fill stream stale/disconnected "
                f"(connected={connected}, age={age:.2f}s, "
                f"max={max_private_stream_age_seconds:.2f}s)"
            ),
        ))

    writable = _journal_writable(journal_path)
    r.append(CheckResult(
        "journal_writable", writable,
        f"decision journal (WAL) path writable: {journal_path}" if writable
        else f"journal path NOT writable: {journal_path} — the WAL is mandatory",
    ))

    if settings.trading_mode is TradingMode.LIVE_SMALL:
        cap_ok = settings.live_small_capital_cap_usd > 0
        r.append(CheckResult(
            "live_small_capital_cap", cap_ok,
            f"live_small cap=${settings.live_small_capital_cap_usd}" if cap_ok
            else "live_small requires live_small_capital_cap_usd > 0",
        ))

    r.append(CheckResult(
        "mode_ladder_validated", lower_rungs_validated,
        "lower ladder rungs validated by live_ladder evidence"
        if lower_rungs_validated
        else "paper/shadow rungs not validated — run the live_ladder evidence check "
             "before live",
    ))

    return ChecklistReport(tuple(r))


def _journal_writable(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".prelive_write_probe"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def run_pre_live_checklist_from_env(settings: Settings | None = None) -> ChecklistReport:
    """Standalone env-driven pre-flight, shared by the CLI and the mainnet
    execution drill. The live session re-checks with live OM state."""
    settings = settings or Settings()
    return run_pre_live_checklist(
        settings=settings,
        risk_config=settings.risk,
        kill_switch_active=Path(os.environ.get("KILL_FILE", "KILL")).exists(),
        has_unresolved_orders=False,
        journal_path=Path(os.environ.get("DECISION_JOURNAL", "logs/decision_journal.jsonl")),
        credentials_present=bool(
            os.environ.get("VNEDGE_EXEC_API_KEY") and os.environ.get("VNEDGE_EXEC_API_SECRET")
        ),
        lower_rungs_validated=os.environ.get("PRE_LIVE_LADDER_ATTESTED", "").lower()
        in {"1", "true", "yes", "on"},
    )


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report = run_pre_live_checklist_from_env()
    for c in report.results:
        mark = "PASS" if c.passed else ("FAIL" if c.critical else "WARN")
        print(f"  [{mark}] {c.name}: {c.detail}")
    print("\n" + ("CLEARED — preconditions met; live still requires the three gates + adapter confirm"
                  if report.cleared else "BLOCKED — do NOT enable live until the failures above are green"))
    return 0 if report.cleared else 1


if __name__ == "__main__":
    raise SystemExit(main())
