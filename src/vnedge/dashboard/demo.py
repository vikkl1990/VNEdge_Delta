"""Dashboard demo: paper-replay session feeding the read-only dashboard.

    DASHBOARD_TOKEN=<secret> python -m vnedge.dashboard.demo

Replays real ingested BTC candles (falls back to synthetic data if none are
downloaded) through the REAL pipeline — strategy → gateway → order manager →
paper broker — at several bars per second, publishing a state snapshot each
bar. Nothing here is a demo shortcut except the pacing: the orders on screen
went through the same code live trading will use.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pandas as pd
import uvicorn

from vnedge.config.risk_config import RiskConfig
from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.state_snapshot import FeedHealth, build_snapshot
from vnedge.data.parquet_store import ParquetStore
from vnedge.data.schemas import normalize_candles
from vnedge.execution.journal import DecisionJournal
from vnedge.execution.order_manager import OrderManager
from vnedge.paper.fill_model import FillModel
from vnedge.paper.paper_broker import PaperBroker
from vnedge.paper.simulated_exchange import SimulatedExchange
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import PreTradeRiskGateway
from vnedge.runtime.paper_runner import PaperRunner
from vnedge.runtime.runner_config import RunnerConfig, RunnerMode
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent

logging.basicConfig(level=logging.WARNING)

SYMBOL = "BTC/USDT:USDT"
HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
TOKEN = os.environ.get("DASHBOARD_TOKEN", "vnedge-demo")
BARS_PER_SECOND = float(os.environ.get("DEMO_BARS_PER_SECOND", "4"))


class DemoSmaCross(BaseStrategy):
    """Trades often enough to make the dashboard move. Demo only."""

    strategy_id = "demo_sma_cross"
    warmup_bars = 48

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["sma_fast"] = df["close"].rolling(12).mean()
        df["sma_slow"] = df["close"].rolling(48).mean()
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row, prev = df.iloc[index], df.iloc[index - 1]
        if prev["sma_fast"] <= prev["sma_slow"] and row["sma_fast"] > row["sma_slow"]:
            c = float(row["close"])
            return SignalIntent("long", stop_price=c * 0.98,
                                take_profit_price=c * 1.03, reason="demo sma cross up")
        if prev["sma_fast"] >= prev["sma_slow"] and row["sma_fast"] < row["sma_slow"]:
            c = float(row["close"])
            return SignalIntent("short", stop_price=c * 1.02,
                                take_profit_price=c * 0.97, reason="demo sma cross down")
        return None


def load_candles() -> pd.DataFrame:
    try:
        return ParquetStore("data").read_candles("binanceusdm", SYMBOL, "1h")
    except FileNotFoundError:
        # Synthetic fallback so the dashboard runs on a fresh clone.
        base, price, raw = 1_750_000_000_000, 60_000.0, []
        for i in range(3000):
            drift = 0.002 if (i // 200) % 2 == 0 else -0.0015
            new = price * (1 + drift * ((i * 7919) % 13 - 6) / 6)
            raw.append([base + i * 3_600_000, price,
                        max(price, new) * 1.003, min(price, new) * 0.997, new, 10.0])
            price = new
        return normalize_candles(raw)


async def main() -> None:
    provider = SnapshotProvider()
    app = create_app(
        provider,
        token=TOKEN,
        snapshot_hz=2.0,
        research_path=Path("research/live_research/latest.json"),
        alpha_council_path=Path("research/live_research/alpha_council_latest.json"),
        alpha_workbench_path=Path("research/live_research/alpha_workbench_latest.json"),
        vibe_intelligence_path=Path("research/live_research/vibe_intelligence_latest.json"),
        realtime_scanner_path=Path("research/live_research/realtime_scanner_latest.json"),
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    )
    candles = load_candles()

    async def replay_forever() -> None:
        while True:
            config = RunnerConfig(
                mode=RunnerMode.PAPER, symbol=SYMBOL,
                starting_equity_usd=500.0,
                risk=RiskConfig(max_daily_loss_usd=1_000.0, max_daily_loss_pct=10.0,
                                max_consecutive_losses=20),  # demo: keep it moving
            )
            exchange = SimulatedExchange(FillModel(), config.starting_equity_usd)
            journal = DecisionJournal(Path("logs/dashboard_demo_journal.jsonl"))
            kill = KillSwitch(kill_file=Path("logs/DEMO_KILL"))
            gateway = PreTradeRiskGateway(config.risk, kill)
            om = OrderManager(gateway, journal, PaperBroker(exchange))
            tracker_holder = {}

            async def on_bar(index: int, ts) -> None:
                runner = tracker_holder["runner"]
                provider.publish(
                    build_snapshot(
                        mode="paper (demo replay)", live_trading_enabled=False,
                        symbol=SYMBOL, strategy_id="demo_sma_cross",
                        tracker=runner.tracker, exchange=exchange,
                        kill_switch=kill, journal=journal, order_manager=om,
                        feed_health=FeedHealth(
                            exchange="binanceusdm (replay)",
                            last_update_ms=1000.0 / BARS_PER_SECOND,
                        ),
                        quote=exchange.quotes.get(SYMBOL),
                        funding_rate=0.0001,
                        session_stats={
                            "started_at": "2026-07-03T00:00:00+00:00",
                            "bars_processed": index,
                            "signals": runner.signals,
                            "orders_submitted": runner.orders_submitted,
                            "risk_rejects": runner.risk_rejects,
                            "sizing_skips": runner.sizing_skips,
                            "recon_mismatches": runner.recon_mismatches,
                            "dropped_candles": 0,
                        },
                        trial={
                            "trial_id": "demo_replay", "started": "2026-07-03",
                            "min_days": 14, "preferred_days": 30, "min_trades": 10,
                            "max_dd_pct": 6.0, "daily_stop_usd": 10.0,
                            "promotion_source": "demo",
                        },
                    )
                )
                await asyncio.sleep(1.0 / BARS_PER_SECOND)

            runner = PaperRunner(
                DemoSmaCross(), candles, None, config,
                gateway=gateway, order_manager=om, exchange=exchange,
                journal=journal, on_bar=on_bar,
            )
            tracker_holder["runner"] = runner
            report = await runner.run()
            print("demo replay pass finished:", report.summary)

    print(f"VNEDGE dashboard: http://{HOST}:{PORT}/?token={TOKEN}")
    await asyncio.gather(server.serve(), replay_forever())


if __name__ == "__main__":
    asyncio.run(main())
