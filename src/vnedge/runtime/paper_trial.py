"""Governed live-data paper trial runner.

    python -m vnedge.runtime.paper_trial research/paper_trials/<trial>.yaml \
        --hours 24 [--dashboard]

Loads a locked trial manifest, refuses anything that isn't a pure paper
trial (live orders in a manifest are a validation error, not a setting),
seeds warmup history via REST, then runs the existing LivePaperSession —
strategy → gateway → journal → OrderManager → PaperBroker — on live
websocket data. Each session run appends a report (with manifest id and
source commit) to the trial's reports.jsonl, so a multi-day trial is a
sequence of journaled, attributable runs.

The trial runner adds NO new execution path and NO new risk logic — limits
come from the manifest into the same RiskConfig the gateway already
enforces (daily loss binds at min(fixed, pct of peak)).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yaml

from vnedge.config.risk_config import RiskConfig
from vnedge.execution.fill_ledger import FillLedger
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.governance.promotion_policy import DEFAULT_PROMOTION_POLICY
from vnedge.governance.proofs import sha256_json, sha256_text
from vnedge.governance.signed_envelope import (
    NonceReplayStore,
    SignedPaperEligibilityEnvelope,
    load_governance_keyring,
    load_signed_paper_envelope,
)
from vnedge.paper.account_store import PaperAccountStore
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.research.strategy_evidence_registry import (
    DEFAULT_REGISTRY,
    build_registry_snapshot,
    route_cost_contract,
    strategy_authority_blockers,
)
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.live_paper import LivePaperSession
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class TrialManifest:
    trial_id: str
    strategy: str
    exchange: str
    symbol: str
    timeframe: str
    mode: str
    strategy_params: dict
    cost_model: dict
    cost_contract_id: str
    strategy_registry_path: str
    starting_equity: float
    daily_loss_limit_usd: float
    max_daily_loss_pct: float
    live_orders_enabled: bool
    promotion_source_commit: str
    eligibility_proof_path: str
    eligibility_envelope: SignedPaperEligibilityEnvelope

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        keyring_path: Path | None = None,
    ) -> TrialManifest:
        raw = yaml.safe_load(path.read_text())
        proof_locator = str(raw.get("eligibility_proof_path") or "").strip()
        if not proof_locator:
            raise ValueError(
                "paper trial requires eligibility_proof_path; string approvals are not authority"
            )
        proof_path = Path(proof_locator)
        if not proof_path.is_absolute():
            proof_path = path.parent / proof_path
        envelope = load_signed_paper_envelope(proof_path)
        local_keyring = path.parent / "governance_keyring.json"
        trust_path = keyring_path or Path(
            os.environ.get(
                "VNEDGE_GOVERNANCE_KEYRING",
                str(local_keyring if local_keyring.is_file() else "config/governance_keyring.json"),
            )
        )
        keyring = load_governance_keyring(trust_path)
        configured_registry = raw.get("strategy_registry_path")
        registry_locator = str(configured_registry or DEFAULT_REGISTRY)
        registry_path = Path(registry_locator)
        if configured_registry and not registry_path.is_absolute():
            registry_path = path.parent / registry_path
        registry = build_registry_snapshot(registry_path)
        cost_contract_id = str(raw.get("cost_contract") or "").strip()
        if not cost_contract_id:
            raise ValueError("paper manifest requires exactly one cost_contract ID")
        if "cost_model" in raw:
            raise ValueError(
                "paper manifest must not embed cost_model; resolve cost_contract from canonical registry"
            )
        contract = route_cost_contract(cost_contract_id, registry_path=registry_path)
        cost_model = contract.paper_cost_model()
        manifest = cls(
            trial_id=raw["trial_id"],
            strategy=raw["strategy"],
            exchange=str(raw.get("exchange") or "").strip().lower(),
            symbol=raw["symbol"],
            timeframe=raw["timeframe"],
            mode=raw["mode"],
            strategy_params=raw.get("strategy_params", {}),
            cost_model=cost_model,
            cost_contract_id=cost_contract_id,
            strategy_registry_path=str(registry_path),
            starting_equity=float(raw["starting_equity"]),
            daily_loss_limit_usd=float(raw["daily_loss_limit_usd"]),
            max_daily_loss_pct=float(raw.get("max_daily_loss_pct", 2.0)),
            live_orders_enabled=bool(raw["live_orders_enabled"]),
            promotion_source_commit=str(raw["promotion_source_commit"]),
            eligibility_proof_path=str(proof_path),
            eligibility_envelope=envelope,
        )
        if manifest.live_orders_enabled:
            raise ValueError("manifest enables live orders — not a paper trial, refusing")
        if manifest.exchange not in {"delta_india", "binanceusdm", "bybit"}:
            raise ValueError("paper manifest requires an explicit supported exchange")
        if manifest.mode != "live_data_paper":
            raise ValueError(f"unsupported trial mode '{manifest.mode}'")
        failures = envelope.verify(
            keyring=keyring,
            policy=DEFAULT_PROMOTION_POLICY,
            strategy_id=manifest.strategy,
            symbol=manifest.symbol,
        )
        artifact_map = {
            artifact.name: artifact.sha256 for artifact in envelope.proof.artifacts
        }
        if artifact_map.get("source_commit") != sha256_text(
            manifest.promotion_source_commit
        ):
            failures += ("paper manifest source commit is not bound to eligibility proof",)
        if artifact_map.get("strategy_config") != sha256_json(
            manifest.strategy_params
        ):
            failures += ("paper manifest strategy parameters are not bound to eligibility proof",)
        if artifact_map.get("cost_model") != sha256_json(manifest.cost_model):
            failures += ("paper manifest cost model is not bound to eligibility proof",)
        if artifact_map.get("cost_contract") != sha256_text(manifest.cost_contract_id):
            failures += ("paper manifest cost contract is not bound to eligibility proof",)
        if artifact_map.get("strategy_registry") != hashlib.sha256(
            registry_path.read_bytes()
        ).hexdigest():
            failures += ("paper manifest canonical registry is not bound to eligibility proof",)
        strategy_entry = registry.get("strategies", {}).get(manifest.strategy)
        if not isinstance(strategy_entry, dict):
            failures += ("paper strategy is absent from canonical registry",)
        else:
            if strategy_entry.get("cost_contract") != manifest.cost_contract_id:
                failures += ("paper cost contract differs from canonical strategy registry",)
            evidence = strategy_entry.get("evidence") or {}
            if artifact_map.get("strategy_evidence") != str(
                evidence.get("actual_sha256") or ""
            ):
                failures += ("paper proof is not bound to canonical strategy evidence",)
        failures += strategy_authority_blockers(
            manifest.strategy,
            purpose="paper",
            registry_path=registry_path,
        )
        if artifact_map.get("exchange") != sha256_text(manifest.exchange):
            failures += ("paper manifest exchange is not bound to eligibility proof",)
        if failures:
            raise ValueError("paper eligibility verification failed: " + "; ".join(failures))
        return manifest


class LiveFundingMR(FundingMeanReversion):
    """FundingMeanReversion whose funding series grows with the live feed.

    The seed comes from REST history. Each prepare() extends it with the
    feed's SETTLED funding prints (``feed.funding_events``, refreshed
    periodically) so the live series is the exact construction research
    validated — settled 8h prints, as-of merged. Backward as-of merge
    semantics are unchanged — still strictly causal.

    Falling back to appending the feed's current rate at the newest bar
    happens ONLY when the venue exposes no settled prints. That fallback is a
    DIFFERENT series than research used (predicted, sampled per-bar): it kept
    live funding_pct systematically off the researched values — replayed
    signals from 2026-07-04 that never fired live traced back to exactly this
    divergence. Venues with funding history must never take the fallback.
    """

    def __init__(self, seed_funding: pd.DataFrame, feed, **params) -> None:
        super().__init__(seed_funding, **params)
        self._feed = feed

    def _merge_settled_events(self, events: list[tuple[int, float]]) -> bool:
        """Fold fresh settled prints into the funding series; True if used."""
        if not events:
            return False
        add = pd.DataFrame(events, columns=["ts_ms", "funding_rate"])
        add["timestamp"] = pd.to_datetime(add["ts_ms"], unit="ms", utc=True).astype(
            self.funding["timestamp"].dtype if not self.funding.empty else "datetime64[ms, UTC]"
        )
        merged = pd.concat(
            [self.funding, add[["timestamp", "funding_rate"]]], ignore_index=True
        )
        self.funding = (
            merged.drop_duplicates("timestamp", keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        return True

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        if not self._merge_settled_events(getattr(self._feed, "funding_events", [])):
            # venue exposes no settled prints — accumulate the current rate
            newest = candles["timestamp"].iloc[-1]
            if (
                self._feed.funding_rate is not None
                and not self.funding.empty
                and newest > self.funding["timestamp"].iloc[-1]
            ):
                self.funding = pd.concat(
                    [self.funding, pd.DataFrame(
                        [{"timestamp": newest, "funding_rate": float(self._feed.funding_rate)}]
                    )],
                    ignore_index=True,
                )
        return super().prepare(candles)


def build_trial_session(
    manifest: TrialManifest,
    feed,
    history: pd.DataFrame,
    seed_funding: pd.DataFrame,
    *,
    journal_dir: Path,
    snapshot_provider=None,
) -> LivePaperSession:
    """Wire the trial world. Pure function of its inputs — fully testable."""
    risk = RiskConfig(
        max_daily_loss_usd=manifest.daily_loss_limit_usd,
        max_daily_loss_pct=manifest.max_daily_loss_pct,
    )
    config = RunnerConfig(
        mode=RunnerMode.PAPER, symbol=manifest.symbol,
        timeframe=manifest.timeframe,
        starting_equity_usd=manifest.starting_equity, risk=risk,
    )
    strategy = LiveFundingMR(seed_funding, feed, **manifest.strategy_params)
    exchange = SimulatedExchange(
        FillModel(
            slippage_bps=float(manifest.cost_model["slippage_bps_per_leg"]),
            taker_fee_bps=float(manifest.cost_model["taker_fee_bps"]),
            maker_fee_bps=float(manifest.cost_model["maker_fee_bps"]),
        ),
        config.starting_equity_usd,
    )
    journal = DecisionJournal(journal_dir / f"{manifest.trial_id}.journal.jsonl")
    kill = KillSwitch(kill_file=journal_dir / f"{manifest.trial_id}.KILL")
    gateway = PreTradeRiskGateway(config.risk, kill)
    om = OrderManager(gateway, journal, PaperBroker(exchange))
    from vnedge.monitoring.alerts import AlertEngine, default_trial_rules
    from vnedge.monitoring.notifiers import LogNotifier, TelegramNotifier

    notifiers: list = [LogNotifier()]
    telegram = TelegramNotifier.from_env()
    if telegram is not None:
        notifiers.append(telegram)
        logger.info("telegram alerts enabled")
    alert_engine = AlertEngine(
        default_trial_rules(manifest.daily_loss_limit_usd),
        journal_dir / f"{manifest.trial_id}.alerts.jsonl",
        notifiers,
    )
    session = LivePaperSession(
        strategy, feed, history, config,
        gateway=gateway, order_manager=om, exchange=exchange, journal=journal,
        snapshot_provider=snapshot_provider,
        account_store=PaperAccountStore(
            journal_dir / f"{manifest.trial_id}.account.json", manifest.trial_id
        ),
        alert_engine=alert_engine,
        equity_history_path=journal_dir / f"{manifest.trial_id}.equity.jsonl",
        fill_ledger=FillLedger(journal_dir / f"{manifest.trial_id}.fills.jsonl"),
        trial_meta={
            "trial_id": manifest.trial_id,
            "started": "2026-07-03",
            "min_days": 14,
            "preferred_days": 30,
            "min_trades": 10,
            "max_dd_pct": 6.0,
            "daily_stop_usd": manifest.daily_loss_limit_usd,
            "promotion_source": manifest.promotion_source_commit,
        },
    )
    # Resume: a restart must continue the trial's account, never reset it.
    # Expectations make a moved/edited store fail closed instead of injecting
    # a wrong-symbol position or absurd balance into the trial.
    resumed = session.account_store.restore_into(
        exchange, session.tracker,
        expected_symbol=manifest.symbol,
        expected_starting_equity=manifest.starting_equity,
    )
    if resumed:
        state = session.account_store.load() or {}
        session.restore_plan(state.get("plan"))
    journal.append("trial_session_start", {
        "trial_id": manifest.trial_id, "resumed": resumed,
        "cost_contract": manifest.cost_contract_id,
        "balance_usd": exchange.balance_usd,
        "open_positions": len(exchange.get_positions()),
    })
    return session


def _current_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — attribution is best-effort
        return "unknown"


def append_trial_report(manifest: TrialManifest, report, reports_path: Path) -> None:
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "trial_id": manifest.trial_id,
        "manifest_strategy": manifest.strategy,
        "exchange": manifest.exchange,
        "cost_contract": manifest.cost_contract_id,
        "cost_model": manifest.cost_model,
        "promotion_source_commit": manifest.promotion_source_commit,
        "run_commit": _current_commit(),
        "report": report.to_dict(),
    }
    reports_path.parent.mkdir(parents=True, exist_ok=True)
    with open(reports_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


async def _seed_via_rest(manifest: TrialManifest, warmup_hours: int = 450):
    from vnedge.data.ccxt_client import CcxtPublicClient
    from vnedge.data.schemas import normalize_candles, normalize_funding

    until = int(time.time() * 1000)
    async with CcxtPublicClient(manifest.exchange) as rest:
        raw_c = await rest.fetch_candles(
            manifest.symbol, manifest.timeframe, until - warmup_hours * 3_600_000, until
        )
        raw_f = await rest.fetch_funding_history(
            manifest.symbol, until - warmup_hours * 3_600_000, until
        )
    return normalize_candles(raw_c), normalize_funding(raw_f)


async def run_trial(manifest_path: Path, hours: float, dashboard: bool) -> int:
    manifest = TrialManifest.load(manifest_path)
    journal_dir = Path(os.environ.get("VNEDGE_PAPER_JOURNAL_DIR", "logs/paper_trials"))
    nonce_store = NonceReplayStore(
        Path(os.environ.get("VNEDGE_GOVERNANCE_NONCE_DB", "data/governance_nonces.sqlite3"))
    )
    if not nonce_store.consume(
        nonce=manifest.eligibility_envelope.nonce,
        proof_hash=manifest.eligibility_envelope.proof.proof_hash,
        purpose=f"paper_trial:{manifest.trial_id}",
    ):
        raise ValueError("paper eligibility nonce was already consumed; replay refused")
    logger.info("trial %s: seeding warmup history via REST", manifest.trial_id)
    history, seed_funding = await _seed_via_rest(manifest)

    from vnedge.exchange.live_feed import LiveMarketFeed

    feed = LiveMarketFeed(
        manifest.exchange, symbol=manifest.symbol, timeframe=manifest.timeframe
    )
    provider = None
    server_task = None
    if dashboard:
        import uvicorn

        from vnedge.dashboard.app import SnapshotProvider, create_app
        from vnedge.dashboard.auth import TokenStore

        provider = SnapshotProvider()
        app = create_app(
            # DASHBOARD_USERS (per-user tokens) + legacy DASHBOARD_TOKEN
            provider, token_store=TokenStore.from_env(),
            history_path=journal_dir / f"{manifest.trial_id}.equity.jsonl",
            research_path=Path("research/live_research/latest.json"),
            alpha_council_path=Path("research/live_research/alpha_council_latest.json"),
            alpha_workbench_path=Path("research/live_research/alpha_workbench_latest.json"),
            vibe_intelligence_path=Path("research/live_research/vibe_intelligence_latest.json"),
            realtime_scanner_path=Path("research/live_research/realtime_scanner_latest.json"),
            alerts_path=Path("logs/alerts.jsonl"),
            journal_dir=journal_dir,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                # inside a container this must bind 0.0.0.0; compose maps it
                # to the HOST's 127.0.0.1 only, so it stays private
                host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
                port=int(os.environ.get("DASHBOARD_PORT", "8080")),
                log_level="warning",
            )
        )
        server_task = asyncio.create_task(server.serve())

    session = build_trial_session(
        manifest, feed, history, seed_funding,
        journal_dir=journal_dir, snapshot_provider=provider,
    )
    await feed.start()
    try:
        report = await session.run(deadline_seconds=hours * 3600)
    finally:
        await feed.stop()
        if server_task is not None:
            server_task.cancel()

    reports_dir = Path(
        os.environ.get("VNEDGE_PAPER_REPORTS_DIR", str(manifest_path.parent))
    )
    reports_path = reports_dir / f"{manifest.trial_id}.reports.jsonl"
    append_trial_report(manifest, report, reports_path)
    print(report.summary)
    print(f"trial report appended to {reports_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="VNEDGE governed paper trial")
    p.add_argument("manifest", type=Path)
    p.add_argument("--hours", type=float, default=24.0, help="session length")
    p.add_argument("--dashboard", action="store_true",
                   help="serve the read-only dashboard "
                        "(DASHBOARD_TOKEN or DASHBOARD_USERS required)")
    args = p.parse_args(argv)
    return asyncio.run(run_trial(args.manifest, args.hours, args.dashboard))


if __name__ == "__main__":
    sys.exit(main())
