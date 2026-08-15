"""Research-only live Delta scalper scanner.

Public WebSocket/REST data in; append-only research decisions and an atomic
dashboard snapshot out.  This process deliberately constructs no account
client, execution adapter, OrderManager, or broker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from vnedge.data.delta_native_history import fetch_delta_candle_history
from vnedge.exchange.delta_contracts import fetch_india_contract_spec
from vnedge.exchange.delta_ws import DeltaPublicWsClient
from vnedge.execution.journal import DecisionJournal
from vnedge.scalping.delta_engine.architecture import architecture_manifest
from vnedge.scalping.delta_engine.candle_store import (
    TIMEFRAME_SECONDS,
    MultiTimeframeCandleStore,
)
from vnedge.scalping.delta_engine.config import DeltaScalperConfig, load_delta_scalper_config
from vnedge.scalping.delta_engine.factory import build_delta_scalper_assembly
from vnedge.scalping.delta_engine.flow_store import FlowSnapshot, L2TradeFlowStore
from vnedge.scalping.delta_engine.forming_candles import MultiTimeframeCandleEngine
from vnedge.scalping.delta_engine.forward_tracker import ForwardOutcomeTracker
from vnedge.scalping.delta_engine.types import Candle, SignalCandidate

DEFAULT_SYMBOLS = ("BTCUSD", "ETHUSD")
DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")


def _proven_decision_time(local_now: datetime, close_ts: datetime) -> datetime:
    """Use exchange rollover proof when the local clock trails the boundary."""
    return max(local_now, close_ts)


def _publish(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


class DeltaScalperShadowService:
    def __init__(
        self,
        symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
        *,
        snapshot_path: Path | str = "research/live_research/delta_scalper_engine_latest.json",
        journal_path: Path | str = "logs/delta_scalper/delta_scalper_shadow.journal.jsonl",
        backtest_path: Path | str = "research/live_research/delta_scalper_backtest_latest.json",
        deto_enabled: bool = False,
        scalper_opted_in: bool = False,
        config: DeltaScalperConfig | None = None,
    ) -> None:
        settings = config or DeltaScalperConfig()
        settings.assert_runtime_safe()
        self.symbols = tuple(symbol.upper() for symbol in symbols)
        self.snapshot_path = Path(snapshot_path)
        self.backtest_path = Path(backtest_path)
        self.store = MultiTimeframeCandleStore(
            max_bars_per_timeframe=settings.features.max_bars_per_timeframe
        )
        self.forming_candles = MultiTimeframeCandleEngine(input_mode="tick")
        self.journal = DecisionJournal(journal_path)
        assembly = build_delta_scalper_assembly(
            self.store,
            settings,
            journal=self.journal,
            deto_enabled=deto_enabled,
            scalper_opted_in=scalper_opted_in,
            forming_candle_engine=self.forming_candles,
        )
        self.context = assembly.context
        self.fee_model = assembly.fee_model
        self.generator = assembly.generator
        self.flow_store = L2TradeFlowStore(
            imbalance_history=settings.features.l2_imbalance_history,
            trade_window_seconds=settings.features.trade_flow_window_seconds,
        )
        self.forward_tracker = ForwardOutcomeTracker(self.fee_model)
        self.recent_outcomes: dict[str, deque[dict]] = {
            symbol: deque(maxlen=500) for symbol in self.symbols
        }
        self.latest_decision: dict[str, dict] = {}
        self.evaluations: dict[str, int] = defaultdict(int)
        self.alerts: dict[str, int] = defaultdict(int)
        self.started_at = datetime.now(UTC)
        self.seed_status: dict[str, dict[str, object]] = {}
        self._gap_backfills: set[tuple[str, str]] = set()
        self._background_tasks: set[asyncio.Task] = set()
        self.ws = DeltaPublicWsClient(
            list(self.symbols),
            candle_timeframes=DEFAULT_TIMEFRAMES,
            on_book=self._on_book,
            on_trade=self._on_trade,
            on_candle=self._on_candle,
        )

    async def seed(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        lookback_days = {"1m": 3, "5m": 5, "15m": 14, "1h": 60, "4h": 120}
        for symbol in self.symbols:
            symbol_status: dict[str, object] = {
                "contract_loaded": False,
                "seeded_bars": {},
                "errors": [],
            }
            self.seed_status[symbol] = symbol_status
            try:
                spec = await asyncio.to_thread(fetch_india_contract_spec, symbol)
                self.flow_store.set_contract_value(symbol, spec.contract_value)
                symbol_status["contract_loaded"] = True
            except Exception as exc:  # noqa: BLE001 - public-data startup boundary
                error = f"contract:{type(exc).__name__}:{exc}"
                errors = symbol_status["errors"]
                assert isinstance(errors, list)
                errors.append(error)
                self.journal.append(
                    "delta_scalper_seed_failed",
                    {"symbol": symbol, "stage": "contract", "error": error},
                )
            for timeframe in DEFAULT_TIMEFRAMES:
                try:
                    frame = await fetch_delta_candle_history(
                        symbol,
                        resolution=timeframe,
                        start_s=int(
                            (current - timedelta(days=lookback_days[timeframe])).timestamp()
                        ),
                        end_s=int(current.timestamp()),
                    )
                    step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
                    seeded = 0
                    for row in frame.itertuples(index=False):
                        closed = Candle(
                            ts=row.timestamp.to_pydatetime() + step,
                            open=float(row.open),
                            high=float(row.high),
                            low=float(row.low),
                            close=float(row.close),
                            volume=float(row.volume),
                            tf=timeframe,
                        )
                        self.store.append_closed(symbol, closed, observed_at=current)
                        seeded += 1
                    seeded_bars = symbol_status["seeded_bars"]
                    assert isinstance(seeded_bars, dict)
                    seeded_bars[timeframe] = seeded
                except Exception as exc:  # noqa: BLE001 - public-data startup boundary
                    error = f"{timeframe}:{type(exc).__name__}:{exc}"
                    errors = symbol_status["errors"]
                    assert isinstance(errors, list)
                    errors.append(error)
                    self.journal.append(
                        "delta_scalper_seed_failed",
                        {"symbol": symbol, "stage": timeframe, "error": error},
                    )

    def _on_book(self, symbol: str, bids: list, asks: list, _raw: dict) -> None:
        raw_sequence = _raw.get("sequence") or _raw.get("sequence_number")
        try:
            sequence = int(raw_sequence) if raw_sequence is not None else None
            snapshot = self.flow_store.on_book(
                symbol,
                bids,
                asks,
                observed_at=datetime.now(UTC),
                sequence=sequence,
            )
        except (TypeError, ValueError):
            return
        self._apply_flow(snapshot)

    def _apply_flow(self, snapshot: FlowSnapshot) -> None:
        self.context.update_l2_confirmation(
            snapshot.symbol,
            imbalance=snapshot.raw_imbalance,
            cvd=snapshot.cvd_usd,
            imbalance_z=snapshot.imbalance_z,
            buy_aggression_ratio=snapshot.buy_aggression_ratio,
            absorption_score=snapshot.absorption_score,
            depth_usd=snapshot.depth_usd,
            sequence_healthy=(
                snapshot.sequence.healthy
                if snapshot.sequence.last_sequence is not None
                else None
            ),
            observed_at=snapshot.observed_at or datetime.now(UTC),
        )

    def _on_trade(self, symbol: str, trade: dict) -> None:
        local_receive_ts = datetime.now(UTC)
        raw_sequence = trade.get("sequence")
        try:
            exchange_ts = datetime.fromtimestamp(
                float(trade.get("ts_ms") or 0) / 1000.0, tz=UTC
            )
            price = float(trade.get("price") or 0)
            size = float(trade.get("size") or 0)
            side = str(trade.get("side") or "")
            sequence = int(raw_sequence) if raw_sequence is not None else None
        except (TypeError, ValueError):
            return
        try:
            self.forming_candles.on_tick(
                symbol,
                exchange_ts=exchange_ts,
                price=price,
                volume=size,
                local_recv_ts=local_receive_ts,
                event_id=(
                    str(trade.get("trade_id") or trade.get("id"))
                    if trade.get("trade_id") is not None
                    or trade.get("id") is not None
                    else (
                        f"{trade.get('ts_ms')}:{raw_sequence}:"
                        f"{trade.get('price')}:{trade.get('size')}"
                        if raw_sequence is not None
                        else None
                    )
                ),
            )
        except ValueError as exc:
            # The confirmed flow store continues to see the event. Forming
            # state is separately fail-closed and never allowed to suppress
            # the existing public-flow telemetry path.
            self.journal.append(
                "delta_forming_candle_event_rejected",
                {
                    "symbol": symbol,
                    "exchange_ts": exchange_ts.isoformat(),
                    "reason": str(exc),
                    "research_only": True,
                },
            )
        try:
            snapshot = self.flow_store.on_trade(
                symbol,
                price=price,
                size=size,
                side=side,
                observed_at=exchange_ts,
                sequence=sequence,
            )
        except (TypeError, ValueError):
            return
        self._apply_flow(snapshot)

    def _on_candle(self, symbol: str, timeframe: str, row: list) -> None:
        now = datetime.now(UTC)
        try:
            close_ts = datetime.fromtimestamp(float(row[0]) / 1000.0, tz=UTC) + timedelta(
                seconds=TIMEFRAME_SECONDS[timeframe]
            )
        except (IndexError, KeyError, TypeError, ValueError):
            return
        latest = self.store.latest(symbol, timeframe)
        # REST seeding can overlap the first WS rollover. The seeded closed bar
        # is already authoritative for research state; never regress or
        # conflict on that hand-off.
        if latest is not None and close_ts <= latest.ts:
            return
        expected_close = (
            latest.ts + timedelta(seconds=TIMEFRAME_SECONDS[timeframe]) if latest else None
        )
        if latest is not None and close_ts > expected_close:
            key = (symbol, timeframe)
            if key not in self._gap_backfills:
                self._gap_backfills.add(key)
                self.journal.append(
                    "delta_scalper_candle_gap",
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "latest_close": latest.ts.isoformat(),
                        "observed_close": close_ts.isoformat(),
                        "action": "rest_backfill_scheduled",
                    },
                )
                task = asyncio.create_task(
                    self._backfill_gap(symbol, timeframe, latest.ts, close_ts),
                    name=f"delta-gap-{symbol}-{timeframe}",
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            return
        if not self.store.from_delta_row(symbol, timeframe, row, observed_at=now):
            return
        if timeframe == "1m":
            closed_bar = self.store.latest(symbol, "1m")
            if closed_bar is not None:
                for outcome in self.forward_tracker.on_closed_bar(symbol, closed_bar):
                    payload = outcome.to_dict()
                    self.recent_outcomes[symbol].append(payload)
                    self.journal.append("delta_scalper_shadow_outcome", payload)
        if timeframe not in self.generator.gates.primary_timeframes:
            return
        # The websocket adapter emits the prior candle only after observing a
        # rollover.  That rollover is authoritative close proof even when the
        # local wall clock is a few milliseconds behind the exchange boundary.
        # Passing the raw local clock here used to make MarketContextBuilder
        # reject a legitimately closed candle as "in the future".
        decision = self.generator.on_candle_closed(
            symbol,
            timeframe,
            now=_proven_decision_time(now, close_ts),
        )
        self.evaluations[symbol] += 1
        if decision.selected is not None:
            self.alerts[symbol] += 1
            self._journal_candidate_forming_context(symbol, decision.selected)
            self.forward_tracker.register(decision.selected)
        self.latest_decision[symbol] = decision.to_dict()
        self.publish()

    def _journal_candidate_forming_context(
        self, symbol: str, candidate: SignalCandidate
    ) -> None:
        """Bind the point-in-time forming state to a selected research candidate."""

        try:
            forming_snapshot = self.forming_candles.snapshot(
                symbol,
                as_of=candidate.decision_ts,
                completed_limit=1,
            ).to_dict()
        except (RuntimeError, ValueError):
            forming_snapshot = None
        self.journal.append(
            "delta_scalper_candidate_forming_context",
            {
                "candidate_key": candidate.dedup_key,
                "symbol": symbol,
                "decision_ts": candidate.decision_ts.isoformat(),
                "forming_candles": forming_snapshot,
                "forming_candles_context_only": True,
                "used_for_signal": False,
                "used_for_execution": False,
            },
        )

    async def _backfill_gap(
        self,
        symbol: str,
        timeframe: str,
        latest_close: datetime,
        observed_close: datetime,
    ) -> None:
        key = (symbol, timeframe)
        try:
            frame = await fetch_delta_candle_history(
                symbol,
                resolution=timeframe,
                start_s=int(latest_close.timestamp()),
                end_s=int(observed_close.timestamp()),
            )
            step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
            restored = 0
            for row in frame.itertuples(index=False):
                closed = Candle(
                    ts=row.timestamp.to_pydatetime() + step,
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=float(row.volume),
                    tf=timeframe,
                )
                latest = self.store.latest(symbol, timeframe)
                if latest is None or closed.ts > latest.ts:
                    self.store.append_closed(symbol, closed, observed_at=observed_close)
                    restored += 1
            self.journal.append(
                "delta_scalper_candle_gap_recovered",
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "restored_bars": restored,
                    "resume_on_next_close": True,
                },
            )
        except (OSError, TimeoutError, TypeError, ValueError) as exc:
            self.journal.append(
                "delta_scalper_candle_gap_recovery_failed",
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        finally:
            self._gap_backfills.discard(key)

    def publish(self) -> None:
        now = datetime.now(UTC)
        try:
            backtest = json.loads(self.backtest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            backtest = {}
        market_evidence = backtest.get("markets") if isinstance(backtest.get("markets"), dict) else {}
        rows = []
        for symbol in self.symbols:
            decision = self.latest_decision.get(symbol)
            selected = decision.get("selected") if decision else None
            try:
                live_context = self.context.build(symbol, now=now)
            except (RuntimeError, ValueError):
                live_context = None
            try:
                point_in_time = self.forming_candles.snapshot(
                    symbol, completed_limit=1
                )
                if now > point_in_time.available_at:
                    point_in_time = self.forming_candles.snapshot(
                        symbol, as_of=now, completed_limit=1
                    )
                forming_state = point_in_time.to_dict()
            except RuntimeError:
                forming_state = None
            evidence = market_evidence.get(symbol, {}) if isinstance(market_evidence, dict) else {}
            evidence_summary = (
                evidence.get("summary") if isinstance(evidence.get("summary"), dict) else {}
            )
            outcomes = tuple(self.recent_outcomes[symbol])
            outcome_net = [float(item["net_bps"]) for item in outcomes]
            outcome_expected = [float(item["expected_net_bps"]) for item in outcomes]
            live_evidence = {
                "completed_alerts": len(outcomes),
                "average_expected_net_bps": (
                    sum(outcome_expected) / len(outcome_expected) if outcome_expected else None
                ),
                "average_realized_net_bps": (
                    sum(outcome_net) / len(outcome_net) if outcome_net else None
                ),
                "expectation_error_bps": (
                    sum(realized - expected for realized, expected in zip(outcome_net, outcome_expected))
                    / len(outcomes)
                    if outcomes
                    else None
                ),
                "positive_rate": (
                    sum(value > 0 for value in outcome_net) / len(outcome_net)
                    if outcome_net
                    else None
                ),
                "scalper_compliance_rate": (
                    sum(bool(item["scalper_compliant"]) for item in outcomes) / len(outcomes)
                    if outcomes
                    else None
                ),
            }
            confirmation = (
                {
                    "status": live_context.l2.status,
                    "imbalance": live_context.l2.imbalance,
                    "imbalance_z": live_context.l2.imbalance_z,
                    "cvd": live_context.l2.cvd,
                    "buy_aggression_ratio": live_context.l2.buy_aggression_ratio,
                    "absorption_score": live_context.l2.absorption_score,
                    "depth_usd": live_context.l2.depth_usd,
                    "sequence_healthy": live_context.l2.sequence_healthy,
                    "context_only": True,
                    "used_for_signal": False,
                    "used_for_execution": False,
                }
                if live_context is not None
                else None
            )
            rows.append(
                {
                    "strategy_id": "delta_scalper_engine_v1",
                    "exchange": "delta_india",
                    "symbol": symbol,
                    "timeframe": "1m/5m + 15m/1h/4h context",
                    "state": "FIRING" if selected else "WAITING",
                    "latest_eval_ts": decision.get("decision_ts") if decision else None,
                    "latest_bar_ts": (
                        self.store.latest(symbol, "1m").ts.isoformat()
                        if self.store.latest(symbol, "1m") else None
                    ),
                    "latest_eval": {
                        "side": selected.get("side") if selected else None,
                        "signal": selected,
                        "l2_confirmation": (
                            selected.get("metadata", {}).get("l2_confirmation")
                            if selected else confirmation
                        ),
                        "active_regime": live_context.regime.value if live_context else "unknown",
                        "scanner_hit_rate": (
                            self.alerts[symbol] / self.evaluations[symbol]
                            if self.evaluations[symbol]
                            else 0.0
                        ),
                        "backtest_average_net_bps": evidence_summary.get("average_net_bps"),
                        "backtest_profit_factor": evidence_summary.get("profit_factor"),
                        "scalper_compliance_rate": evidence_summary.get(
                            "scalper_compliance_rate"
                        ),
                        "forward_evidence": live_evidence,
                        "pipeline_trace": decision.get("pipeline_trace") if decision else [],
                        "pipeline_duration_us": (
                            decision.get("total_duration_us") if decision else None
                        ),
                        "journal_write_success": (
                            decision.get("journal_write_success") if decision else None
                        ),
                        "research_only": True,
                        "can_trade": False,
                        "can_promote": False,
                        "forming_candles": forming_state,
                        "forming_candles_used_for_current_scanners": False,
                    },
                    "why": (
                        "fee-adjusted closed-candle setup passed research gates"
                        if selected else "waiting for a setup that clears probability and fee gates"
                    ),
                    "evaluations": self.evaluations[symbol],
                    "alerts": self.alerts[symbol],
                    "forward_evidence": live_evidence,
                    "can_trade": False,
                    "can_promote": False,
                }
            )
        _publish(
            self.snapshot_path,
            {
                "generated_at": now.isoformat(),
                "mode": "delta_scalper_research_shadow",
                "summary": {
                    "connected_symbols": len(self.symbols),
                    "firing": sum(row["state"] == "FIRING" for row in rows),
                    "waiting": sum(row["state"] == "WAITING" for row in rows),
                    "uptime_seconds": (now - self.started_at).total_seconds(),
                    "evaluations": sum(self.evaluations.values()),
                    "alerts": sum(self.alerts.values()),
                    "completed_forward_outcomes": sum(
                        len(items) for items in self.recent_outcomes.values()
                    ),
                    "enabled_scanners": [
                        scanner.scanner_id for scanner in self.generator.scanners
                    ],
                    "websocket_healthy": self.ws.healthy,
                    "last_market_event_at": (
                        self.ws.last_event_at.isoformat() if self.ws.last_event_at else None
                    ),
                },
                "seed_status": self.seed_status,
                "fee_model": {
                    "maker_bps_including_gst": self.fee_model.maker_bps,
                    "taker_bps_including_gst": self.fee_model.taker_bps,
                    "deto_enabled": self.fee_model.deto_enabled,
                    "scalper_opted_in": self.fee_model.scalper_opted_in,
                },
                "architecture": architecture_manifest(),
                "backtest_summary": backtest.get("summary"),
                "fee_effectiveness": backtest.get("fee_sensitivity"),
                "robust_validation": backtest.get("robust_validation"),
                "untouched_window": backtest.get("untouched_window"),
                "rows": rows,
                "policy": {
                    "research_only": True,
                    "order_route_present": False,
                    "l2_is_confirmation_only": True,
                    "can_trade": False,
                    "can_promote": False,
                },
                "can_trade": False,
                "can_promote": False,
            },
        )

    async def run(self) -> None:
        await self.seed()
        self.publish()
        await self.ws.start()
        try:
            while True:
                for symbol, rate in self.ws.funding_rate.items():
                    self.context.update_funding(symbol, rate, datetime.now(UTC))
                # This heartbeat proves process liveness only. Data freshness
                # remains tied to latest_bar_ts in each row.
                self.publish()
                await asyncio.sleep(5)
        finally:
            for task in self._background_tasks:
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            await self.ws.stop()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/delta_scalper.yaml")
    parser.add_argument("--symbols")
    parser.add_argument(
        "--snapshot", default="research/live_research/delta_scalper_engine_latest.json"
    )
    parser.add_argument("--deto", action="store_true")
    parser.add_argument("--scalper-opted-in", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = load_delta_scalper_config(args.config)
    symbols = (
        tuple(value.strip().upper() for value in args.symbols.split(",") if value.strip())
        if args.symbols
        else config.engine.symbols
    )
    service = DeltaScalperShadowService(
        symbols,
        snapshot_path=args.snapshot,
        deto_enabled=args.deto,
        scalper_opted_in=args.scalper_opted_in,
        config=config,
    )
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
