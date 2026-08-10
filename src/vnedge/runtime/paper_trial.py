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
import json
import logging
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
from vnedge.paper.account_store import PaperAccountStore
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.live_paper import LivePaperSession
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion

logger = logging.getLogger(__name__)

APPROVED_STRATEGIES = {"funding_mean_reversion_v1"}


@dataclass(frozen=True)
class TrialManifest:
    trial_id: str
    strategy: str
    symbol: str
    timeframe: str
    mode: str
    approved_by: str
    strategy_params: dict
    starting_equity: float
    daily_loss_limit_usd: float
    max_daily_loss_pct: float
    live_orders_enabled: bool
    promotion_source_commit: str

    @classmethod
    def load(cls, path: Path) -> "TrialManifest":
        raw = yaml.safe_load(path.read_text())
        manifest = cls(
            trial_id=raw["trial_id"],
            strategy=raw["strategy"],
            symbol=raw["symbol"],
            timeframe=raw["timeframe"],
            mode=raw["mode"],
            approved_by=raw["approved_by"],
            strategy_params=raw.get("strategy_params", {}),
            starting_equity=float(raw["starting_equity"]),
            daily_loss_limit_usd=float(raw["daily_loss_limit_usd"]),
            max_daily_loss_pct=float(raw.get("max_daily_loss_pct", 2.0)),
            live_orders_enabled=bool(raw["live_orders_enabled"]),
            promotion_source_commit=str(raw["promotion_source_commit"]),
        )
        if manifest.live_orders_enabled:
            raise ValueError("manifest enables live orders — not a paper trial, refusing")
        if manifest.mode != "live_data_paper":
            raise ValueError(f"unsupported trial mode '{manifest.mode}'")
        if manifest.strategy not in APPROVED_STRATEGIES:
            raise ValueError(
                f"strategy '{manifest.strategy}' has no promotion-gate approval on record"
            )
        if manifest.approved_by != "human":
            raise ValueError("paper trials require human approval on the manifest")
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
    exchange = SimulatedExchange(FillModel(), config.starting_equity_usd)
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
    async with CcxtPublicClient("binanceusdm") as rest:
        raw_c = await rest.fetch_candles(
            manifest.symbol, manifest.timeframe, until - warmup_hours * 3_600_000, until
        )
        raw_f = await rest.fetch_funding_history(
            manifest.symbol, until - warmup_hours * 3_600_000, until
        )
    return normalize_candles(raw_c), normalize_funding(raw_f)


async def run_trial(manifest_path: Path, hours: float, dashboard: bool) -> int:
    manifest = TrialManifest.load(manifest_path)
    logger.info("trial %s: seeding warmup history via REST", manifest.trial_id)
    history, seed_funding = await _seed_via_rest(manifest)

    from vnedge.exchange.live_feed import LiveMarketFeed

    feed = LiveMarketFeed(
        "binanceusdm", symbol=manifest.symbol, timeframe=manifest.timeframe
    )
    provider = None
    server_task = None
    if dashboard:
        import os

        import uvicorn

        from vnedge.dashboard.app import SnapshotProvider, create_app
        from vnedge.dashboard.auth import TokenStore

        provider = SnapshotProvider()
        app = create_app(
            # DASHBOARD_USERS (per-user tokens) + legacy DASHBOARD_TOKEN
            provider, token_store=TokenStore.from_env(),
            history_path=Path("logs/paper_trials") / f"{manifest.trial_id}.equity.jsonl",
            research_path=Path("research/live_research/latest.json"),
            alpha_council_path=Path("research/live_research/alpha_council_latest.json"),
            alpha_workbench_path=Path("research/live_research/alpha_workbench_latest.json"),
            vibe_intelligence_path=Path("research/live_research/vibe_intelligence_latest.json"),
            realtime_scanner_path=Path("research/live_research/realtime_scanner_latest.json"),
            alerts_path=Path("logs/alerts.jsonl"),
            journal_dir=Path("logs/paper_trials"),
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

    journal_dir = Path("logs/paper_trials")
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

    reports_path = manifest_path.parent / f"{manifest.trial_id}.reports.jsonl"
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
