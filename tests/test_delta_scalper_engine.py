"""Delta scalper engine contracts, costs, causality, and safety routing."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from vnedge.config.risk_config import RiskConfig
from vnedge.risk.kill_switch import KillSwitch
from vnedge.risk.risk_manager import AccountState, PreTradeRiskGateway
from vnedge.scalping.delta_engine.architecture import architecture_manifest
from vnedge.scalping.delta_engine.backtester import CausalScalperBacktester
from vnedge.scalping.delta_engine.candle_store import (
    ClosedCandleAggregator,
    MultiTimeframeCandleStore,
)
from vnedge.scalping.delta_engine.config import DeltaScalperConfig, load_delta_scalper_config
from vnedge.scalping.delta_engine.context import MarketContextBuilder
from vnedge.scalping.delta_engine.factory import build_delta_scalper_assembly
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel
from vnedge.scalping.delta_engine.flow_store import ChannelSequenceTracker, L2TradeFlowStore
from vnedge.scalping.delta_engine.forward_tracker import ForwardOutcomeTracker
from vnedge.scalping.delta_engine.regime import (
    RegimeProfileConfig,
    build_features,
    build_regime_profile,
)
from vnedge.scalping.delta_engine.scanners import (
    MomentumBurstConfig,
    MomentumBurstScanner,
    Scanner,
)
from vnedge.scalping.delta_engine.signal_generator import (
    DeltaScalperSignalGenerator,
    EngineDecision,
    ScalperRiskAdapter,
    SignalGateConfig,
)
from vnedge.scalping.delta_engine.types import (
    Candle,
    L2Confirmation,
    MarketContext,
    Regime,
    SessionRegime,
    Side,
    SignalCandidate,
    TrendStrength,
    VolatilityRegime,
)
from vnedge.scalping.delta_engine.validation import (
    fee_sensitivity,
    robust_validation_report,
    untouched_window_summary,
)
from vnedge.scalping.microstructure import MarketMicroState, PrivateStreamState, TopOfBook
from vnedge.scalping.risk import ScalperRiskGateway

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def candle(ts: datetime, *, close: float = 100.0, tf: str = "1m") -> Candle:
    return Candle(ts, close - 0.2, close + 0.5, close - 0.5, close, 100.0, tf)


@pytest.mark.parametrize(
    ("deto", "maker", "hold", "expected_fee", "eligible"),
    [
        (False, True, 1_200, 2.36, True),
        (False, False, 1_200, 5.90, True),
        (True, True, 1_200, 1.77, True),
        (True, False, 1_200, 4.425, True),
        (False, True, 1_801, 8.26, False),
        (True, False, 1_801, 8.85, False),
    ],
)
def test_delta_fee_model_all_offer_and_deto_routes(deto, maker, hold, expected_fee, eligible):
    model = DeltaFeeModel(
        deto_enabled=deto,
        scalper_opted_in=True,
        default_slippage_bps_per_leg=0.0,
    )
    result = model.breakdown("BTCUSD", entry_is_maker=maker, hold_seconds=hold)
    assert result.total_bps == pytest.approx(expected_fee)
    assert result.scalper_eligible is eligible


def test_checked_in_config_is_valid_and_cannot_unlock_live():
    config = load_delta_scalper_config()
    assert config.engine.symbols == ("BTCUSD", "ETHUSD")
    assert config.scanners.momentum_burst.enabled_regimes == (
        Regime.QUIET,
        Regime.TRENDING_UP,
        Regime.TRENDING_DOWN,
        Regime.EXPANDING,
        Regime.FUNDING_EXTREME,
    )
    assert config.scanners.imbalance_fade.enabled_regimes == (
        Regime.QUIET,
        Regime.EXPANDING,
    )
    assert config.features.regime_profile_timeframe == "5m"
    assert config.features.regime_profile_strong_trend_adx == 30.0
    assert config.features.regime_profile_high_vol_percentile == 0.75
    assert not config.engine.live_orders_enabled
    with pytest.raises(ValueError, match="research-only"):
        DeltaScalperConfig.model_validate(
            {
                "engine": {
                    "mode": "live",
                    "live_orders_enabled": True,
                }
            }
        )


def test_engine_config_rejects_empty_symbols_and_primary_timeframes():
    with pytest.raises(ValueError, match="symbol"):
        DeltaScalperConfig.model_validate({"engine": {"symbols": []}})
    with pytest.raises(ValueError, match="primary timeframe"):
        DeltaScalperConfig.model_validate({"engine": {"primary_timeframes": []}})


def test_shared_assembly_applies_yaml_scanner_fee_and_timeframe_settings():
    config = DeltaScalperConfig.model_validate(
        {
            "engine": {"symbols": ["BTCUSD"], "primary_timeframes": ["5m"]},
            "fee_model": {"default_slippage_bps_per_leg": 2.3},
            "scanners": {
                "momentum_burst": {"enabled": False},
                "imbalance_fade": {"enabled": True, "min_wick_ratio": 0.61},
            },
        }
    )
    assembly = build_delta_scalper_assembly(MultiTimeframeCandleStore(), config)
    assert [scanner.scanner_id for scanner in assembly.scanners] == [
        "delta_imbalance_fade_v1"
    ]
    assert assembly.generator.gates.primary_timeframes == ("5m",)
    assert assembly.fee_model.default_slippage_bps_per_leg == 2.3


def test_architecture_manifest_reports_deployed_research_boundary():
    manifest = architecture_manifest()
    assert manifest["runtime"]["process_model"] == "single_process_asyncio_research_sidecar"
    assert manifest["runtime"]["offline_replay_uses_live_modules"] is True
    assert manifest["components"]["existing_risk_gateway_adapter"] == (
        "available_not_invoked"
    )
    assert manifest["safety"]["order_route_present"] is False
    assert manifest["safety"]["can_trade"] is False


def test_scalper_offer_is_never_assumed_without_opt_in():
    result = DeltaFeeModel(
        scalper_opted_in=False, default_slippage_bps_per_leg=0
    ).breakdown("ETHUSD", entry_is_maker=True, hold_seconds=300)
    assert not result.scalper_eligible
    assert result.total_bps == pytest.approx(2.36 + 5.90)


def test_candle_store_converts_delta_start_to_close_and_rejects_future():
    store = MultiTimeframeCandleStore()
    start = NOW - timedelta(minutes=1)
    row = [start.timestamp() * 1000, 99.0, 101.0, 98.0, 100.0, 25.0]
    assert store.from_delta_row("BTCUSD", "1m", row, observed_at=NOW)
    assert store.latest("BTCUSD", "1m").ts == NOW
    assert not store.from_delta_row("BTCUSD", "1m", row, observed_at=NOW)
    with pytest.raises(ValueError, match="not closed"):
        store.append_closed("BTCUSD", candle(NOW + timedelta(minutes=1)), observed_at=NOW)


def test_delta_rollover_is_close_proof_despite_small_local_clock_skew():
    store = MultiTimeframeCandleStore()
    start = NOW - timedelta(minutes=1)
    row = [start.timestamp() * 1000, 99.0, 101.0, 98.0, 100.0, 25.0]
    assert store.from_delta_row(
        "BTCUSD", "1m", row, observed_at=NOW - timedelta(milliseconds=500)
    )


def test_closed_candle_aggregator_emits_only_complete_higher_bar():
    agg = ClosedCandleAggregator(("5m",))
    emitted = []
    for minute in range(1, 6):
        emitted.extend(
            agg.on_one_minute(
                "BTCUSD",
                candle(NOW.replace(minute=0) + timedelta(minutes=minute), close=100 + minute),
            )
        )
    assert len(emitted) == 1
    assert emitted[0].tf == "5m"
    assert emitted[0].close == 105
    assert emitted[0].ts.minute == 5


def test_candle_store_closed_callback_and_hld_aliases():
    store = MultiTimeframeCandleStore()
    observed = []
    store.on_closed_candle(lambda symbol, row: observed.append((symbol, row.ts)))
    store.append_closed("btcusd", candle(NOW), observed_at=NOW)
    assert observed == [("BTCUSD", NOW)]
    assert store.get_latest_closed("BTCUSD", "1m") == candle(NOW)
    assert store.get_candles("BTCUSD", "1m", 1) == (candle(NOW),)


def test_sequence_tracker_records_gaps_and_regressions():
    tracker = ChannelSequenceTracker()
    tracker.observe("l2", "BTCUSD", 10)
    gap = tracker.observe("l2", "BTCUSD", 13)
    regression = tracker.observe("l2", "BTCUSD", 12)
    assert gap.gaps == 2
    assert regression.regressions == 1
    assert not regression.healthy


def test_l2_trade_flow_store_computes_causal_confirmation_features():
    store = L2TradeFlowStore(imbalance_history=10, trade_window_seconds=15)
    bids = [
        {"limit_price": "99.9", "size": "10"},
        {"limit_price": "99.8", "size": "8"},
    ]
    asks = [
        {"limit_price": "100.1", "size": "3"},
        {"limit_price": "100.2", "size": "2"},
    ]
    store.on_book("BTCUSD", bids, asks, observed_at=NOW, sequence=1)
    store.on_book("BTCUSD", bids, asks, observed_at=NOW, sequence=2)
    snapshot = store.on_trade(
        "BTCUSD", price=100, size=2, side="buy", observed_at=NOW, sequence=1
    )
    assert snapshot.raw_imbalance > 0
    assert snapshot.cvd_usd == 200
    assert snapshot.buy_aggression_ratio == 1
    assert snapshot.depth_usd > 0
    assert snapshot.sequence.healthy


class _NoSignalGenerator:
    def on_candle_closed(self, symbol, timeframe, now=None):
        return EngineDecision(symbol, now, None, (), ())


def test_backtester_reports_and_resets_on_missing_minute():
    store = MultiTimeframeCandleStore()
    rows = [
        candle(NOW),
        candle(NOW + timedelta(minutes=1)),
        candle(NOW + timedelta(minutes=3)),
    ]
    report = CausalScalperBacktester(_NoSignalGenerator(), DeltaFeeModel(), store).run(
        "BTCUSD", rows
    )
    assert report.missing_one_minute_bars == 1
    assert report.data_quality_pass is False


def test_market_context_is_closed_only_and_immutable():
    store = MultiTimeframeCandleStore()
    for i in range(40):
        ts = NOW - timedelta(hours=40 - i)
        store.append_closed("BTCUSD", candle(ts, close=100 + i, tf="1h"), observed_at=NOW)
    for i in range(30):
        ts = NOW - timedelta(minutes=15 * (30 - i))
        store.append_closed("BTCUSD", candle(ts, close=120 + i / 10, tf="15m"), observed_at=NOW)
    for i in range(35):
        ts = NOW - timedelta(minutes=35 - i)
        store.append_closed("BTCUSD", candle(ts, close=125 + i / 10), observed_at=NOW)
    context = MarketContextBuilder(store).build("BTCUSD", now=NOW)
    assert context.ts <= NOW
    with pytest.raises(TypeError):
        context.features["future"] = 1.0


def test_context_marks_old_l2_confirmation_stale():
    store = MultiTimeframeCandleStore()
    store.append_closed("BTCUSD", candle(NOW), observed_at=NOW)
    builder = MarketContextBuilder(store, max_l2_age_seconds=1)
    builder.update_l2_confirmation(
        "BTCUSD", imbalance=0.5, cvd=100, observed_at=NOW - timedelta(seconds=2)
    )
    context = builder.build("BTCUSD", now=NOW)
    assert context.l2.status == "stale"
    assert context.l2.used_for_signal is False


def test_feature_engine_includes_complete_hld_feature_categories():
    rows = tuple(
        candle(NOW - timedelta(minutes=50 - index), close=100 + index * 0.1)
        for index in range(50)
    )
    features = build_features({"1m": rows, "1h": rows[-40:], "4h": rows[-16:]})
    assert {
        "adx_14",
        "bb_width_bps",
        "atr_percentile",
        "relative_volume",
        "volume_delta_proxy",
        "rsi_14",
        "ema_stack_up",
        "context_1h_ema_gap_bps",
        "context_4h_return_bps",
    } <= set(features)


def test_structured_regime_profile_is_causal_and_directional():
    rows = tuple(
        Candle(
            NOW - timedelta(minutes=5 * (219 - index)),
            100 + index * 0.25 - 0.05,
            100 + index * 0.25 + 0.20,
            100 + index * 0.25 - 0.20,
            100 + index * 0.25,
            100,
            "5m",
        )
        for index in range(220)
    )
    profile = build_regime_profile(
        {"5m": rows},
        funding_rate=0.001,
        l2=L2Confirmation(status="fresh", sequence_healthy=True),
        config=RegimeProfileConfig(),
    )

    assert profile.trend is TrendStrength.STRONG_TREND
    assert profile.trend_direction == "up"
    assert profile.volatility in {
        VolatilityRegime.LOW,
        VolatilityRegime.MEDIUM,
        VolatilityRegime.HIGH,
    }
    assert profile.session in set(SessionRegime)
    assert profile.flags["funding_extreme"] is True
    assert profile.flags["l2_healthy"] is True
    assert profile.metrics["history_bars"] == 220
    assert profile.source_timeframe == "5m"


@pytest.mark.parametrize(
    ("recent_half_range", "expected"),
    [(2.0, VolatilityRegime.HIGH), (0.05, VolatilityRegime.LOW)],
)
def test_structured_regime_profile_uses_rolling_atr_percentiles(
    recent_half_range, expected
):
    rows = []
    for index in range(220):
        baseline = 0.5 if expected is VolatilityRegime.LOW else 0.1
        half_range = recent_half_range if index >= 200 else baseline
        rows.append(
            Candle(
                NOW - timedelta(minutes=5 * (219 - index)),
                100.0,
                100.0 + half_range,
                100.0 - half_range,
                100.0,
                100,
                "5m",
            )
        )
    profile = build_regime_profile(
        {"5m": tuple(rows)},
        funding_rate=0.0,
        l2=L2Confirmation(),
    )
    assert profile.trend is TrendStrength.RANGE
    assert profile.volatility is expected


def test_live_context_and_research_use_identical_candle_feature_definitions():
    store = MultiTimeframeCandleStore()
    candles_by_tf = {"1m": [], "15m": [], "1h": [], "4h": []}
    for tf, count, step in (
        ("1m", 50, timedelta(minutes=1)),
        ("15m", 30, timedelta(minutes=15)),
        ("1h", 40, timedelta(hours=1)),
        ("4h", 16, timedelta(hours=4)),
    ):
        for index in range(count):
            row = candle(
                NOW - step * (count - index - 1),
                close=100 + index * 0.1,
                tf=tf,
            )
            store.append_closed("BTCUSD", row, observed_at=NOW)
            candles_by_tf[tf].append(row)
    context = MarketContextBuilder(store).build("BTCUSD", now=NOW)
    direct = build_features({key: tuple(value) for key, value in candles_by_tf.items()})
    for key, value in direct.items():
        assert context.features[key] == pytest.approx(value)


def _momentum_context(l2: float) -> MarketContext:
    rows = tuple(candle(NOW - timedelta(minutes=31 - i), close=100 + i * 0.01) for i in range(30))
    latest = Candle(NOW, 100.2, 101.4, 100.1, 101.3, 1_000.0, "1m")
    return MarketContext(
        symbol="BTCUSD",
        ts=NOW,
        candles={"1m": rows + (latest,)},
        regime=Regime.EXPANDING,
        funding_rate=0.0,
        funding_velocity=0.0,
        l2=L2Confirmation(imbalance=l2, cvd=l2 * 100, status="fresh", observed_at=NOW),
        features={
            "volume_z": 3.0,
            "body_ratio": 0.92,
            "body_direction": 1.0,
            "breakout_up_bps": 8.0,
            "breakout_down_bps": -8.0,
            "atr_bps": 28.0,
        },
    )


def test_l2_never_changes_momentum_trigger_or_side():
    scanner = MomentumBurstScanner(DeltaFeeModel(scalper_opted_in=True))
    agrees = scanner.evaluate(_momentum_context(0.9))
    opposes = scanner.evaluate(_momentum_context(-0.9))
    assert agrees is not None and opposes is not None
    assert agrees.side is opposes.side is Side.LONG
    assert agrees.fee_adjusted_expectancy_bps == opposes.fee_adjusted_expectancy_bps
    assert agrees.metadata["l2_confirmation"]["used_for_signal"] is False
    assert opposes.metadata["l2_confirmation"]["used_for_execution"] is False
    assert agrees.metadata["regime_filter"]["allowed"] is True


def test_scanner_hard_regime_allowlist_blocks_before_candidate_creation():
    scanner = MomentumBurstScanner(
        DeltaFeeModel(scalper_opted_in=True),
        config=MomentumBurstConfig(enabled_regimes=(Regime.QUIET,)),
    )
    assert scanner.evaluate(_momentum_context(0.9)) is None


def _candidate() -> SignalCandidate:
    return SignalCandidate(
        scanner_id="test_scanner",
        symbol="BTCUSD",
        side=Side.LONG,
        decision_ts=NOW,
        entry_price=100.0,
        stop_loss=99.9,
        take_profits=(100.2,),
        time_stop_seconds=1_680,
        expected_hold_seconds=600,
        expected_move_bps=20.0,
        raw_expectancy_bps=16.0,
        modeled_cost_bps=5.36,
        fee_adjusted_expectancy_bps=10.64,
        scalper_probability=0.75,
        confidence=0.8,
    )


class _StaticScanner(Scanner):
    scanner_id = "test_scanner"

    def evaluate(self, ctx):
        return replace(_candidate(), decision_ts=ctx.ts)

    def required_features(self):
        return ()


class _BrokenScanner(Scanner):
    scanner_id = "broken_scanner"

    def evaluate(self, ctx):
        raise RuntimeError("scanner defect")

    def required_features(self):
        return ()


class _RegimeBlockedScanner(_StaticScanner):
    scanner_id = "regime_blocked_scanner"

    def regime_enabled(self, ctx):
        return False

    def evaluate(self, ctx):
        raise AssertionError("regime-blocked scanner must not be evaluated")


class _BrokenBuilder:
    def build(self, symbol, now=None):
        raise RuntimeError("context defect")


class _UnavailableJournal:
    def append(self, kind, payload):
        return False


class _StaticBuilder:
    def build(self, symbol, now=None):
        ts = now or NOW
        return MarketContext(
            symbol=symbol,
            ts=ts,
            candles={"1m": (candle(ts),)},
            regime=Regime.QUIET,
            funding_rate=0,
            funding_velocity=0,
        )


def test_generator_journals_each_decision_key_once():
    generator = DeltaScalperSignalGenerator(
        _StaticBuilder(),
        (_StaticScanner(),),
        gates=SignalGateConfig(min_expectancy_bps=8, min_probability=0.7),
    )
    first = generator.on_candle_closed("BTCUSD", "1m", now=NOW)
    duplicate = generator.on_candle_closed("BTCUSD", "1m", now=NOW)
    assert first.selected is not None
    assert first.regime_profile is not None
    assert first.regime_profile["trend"] == "unknown"
    assert duplicate.selected is None and duplicate.duplicate
    assert [stage.name for stage in first.pipeline_trace] == [
        "context_builder",
        "scanner:test_scanner",
        "fee_probability_confidence_gates",
        "ranking_and_dedup",
    ]
    assert first.total_duration_us >= 0


def test_generator_isolates_scanner_failure_and_keeps_other_scanners_running():
    generator = DeltaScalperSignalGenerator(
        _StaticBuilder(),
        (_BrokenScanner(), _StaticScanner()),
        gates=SignalGateConfig(min_expectancy_bps=8, min_probability=0.7),
    )
    decision = generator.on_candle_closed("BTCUSD", "1m", now=NOW)
    assert decision.selected is not None
    assert "broken_scanner:scanner_error:RuntimeError" in decision.rejection_reasons
    broken = next(stage for stage in decision.pipeline_trace if "broken_scanner" in stage.name)
    assert broken.status == "error"


def test_generator_journals_hard_regime_filter_decision():
    generator = DeltaScalperSignalGenerator(
        _StaticBuilder(),
        (_RegimeBlockedScanner(),),
        gates=SignalGateConfig(min_expectancy_bps=8, min_probability=0.7),
    )
    decision = generator.on_candle_closed("BTCUSD", "1m", now=NOW)
    assert decision.selected is None
    assert decision.rejection_reasons == (
        "regime_blocked_scanner:regime_not_enabled:quiet",
    )
    stage = next(
        item
        for item in decision.pipeline_trace
        if item.name == "scanner:regime_blocked_scanner"
    )
    assert stage.status == "regime_filtered"


def test_generator_turns_context_failure_into_a_journalable_rejection():
    generator = DeltaScalperSignalGenerator(_BrokenBuilder(), (_StaticScanner(),))
    decision = generator.on_candle_closed("BTCUSD", "1m", now=NOW)
    assert decision.selected is None
    assert decision.rejection_reasons == ("context_error:RuntimeError",)
    assert decision.pipeline_trace[0].status == "error"


def test_generator_fails_closed_when_research_journal_is_unavailable():
    generator = DeltaScalperSignalGenerator(
        _StaticBuilder(),
        (_StaticScanner(),),
        journal=_UnavailableJournal(),
        gates=SignalGateConfig(min_expectancy_bps=8, min_probability=0.7),
    )
    decision = generator.on_candle_closed("BTCUSD", "1m", now=NOW)
    assert decision.selected is None
    assert decision.journal_write_success is False
    assert decision.rejection_reasons[-1] == "journal_unavailable"


def test_generator_uses_configured_primary_timeframes():
    generator = DeltaScalperSignalGenerator(
        _StaticBuilder(),
        (_StaticScanner(),),
        gates=SignalGateConfig(primary_timeframes=("5m",)),
    )
    decision = generator.on_candle_closed("BTCUSD", "1m", now=NOW)
    assert decision.selected is None
    assert decision.rejection_reasons == ("non_primary_timeframe",)


def test_forward_tracker_enters_next_bar_and_journals_once():
    fee = DeltaFeeModel(scalper_opted_in=True, default_slippage_bps_per_leg=0)
    tracker = ForwardOutcomeTracker(fee)
    candidate = _candidate()
    assert tracker.register(candidate)
    assert not tracker.register(candidate)
    next_bar = Candle(
        NOW + timedelta(minutes=1),
        100.0,
        100.3,
        100.0,
        100.25,
        10,
        "1m",
    )
    outcomes = tracker.on_closed_bar("BTCUSD", next_bar)
    assert len(outcomes) == 1
    assert outcomes[0].entry_ts == NOW.isoformat()
    assert outcomes[0].entry_price == 100.0
    assert outcomes[0].exit_reason == "target_1"
    assert outcomes[0].gross_bps == pytest.approx(20.0)
    assert outcomes[0].net_bps == pytest.approx(20.0 - 2.36)
    assert outcomes[0].scalper_compliant
    assert outcomes[0].l2_quality == "unavailable"


def test_backtest_rebases_exit_distances_on_next_open_fill():
    store = MultiTimeframeCandleStore()
    generator = DeltaScalperSignalGenerator(
        _StaticBuilder(),
        (_StaticScanner(),),
        gates=SignalGateConfig(min_expectancy_bps=8, min_probability=0.7),
    )
    fee = DeltaFeeModel(
        scalper_opted_in=True, default_slippage_bps_per_leg=0
    )
    rows = [
        Candle(NOW, 99.9, 100.1, 99.8, 100.0, 1, "1m"),
        Candle(
            NOW + timedelta(minutes=1),
            110.0,
            110.3,
            109.95,
            110.2,
            1,
            "1m",
        ),
    ]
    report = CausalScalperBacktester(generator, fee, store).run("BTCUSD", rows)
    assert len(report.trades) == 1
    assert report.trades[0].exit_reason == "target_1"
    assert report.trades[0].gross_bps == pytest.approx(20.0)


def test_risk_adapter_uses_existing_gateway_and_never_submits(tmp_path):
    base = PreTradeRiskGateway(
        RiskConfig(max_spread_bps=10, max_slippage_bps=10),
        KillSwitch(kill_file=tmp_path / "KILL"),
    )
    adapter = ScalperRiskAdapter(ScalperRiskGateway(base))
    top = TopOfBook("BTCUSD", 99.99, 100, 100.01, 100, NOW)
    micro = MarketMicroState(
        top=top,
        private=PrivateStreamState(NOW, connected=True),
        estimated_slippage_bps=1,
    )
    account = AccountState(1_000, 0, 1_000, 0)
    result = adapter.evaluate(
        _candidate(), notional_usd=100, account=account, market=micro, now=NOW
    )
    assert result.intent.time_in_force == "PO"
    assert result.risk.approved
    assert result.submitted is False


def test_robust_validation_requires_a_config_family_then_completes():
    one = robust_validation_report(np.ones((30, 1)), selected_config=0)
    assert one.status == "requires_multiple_preregistered_configs"
    matrix = np.column_stack(
        [np.linspace(-0.01, 0.02, 60), np.linspace(0.01, -0.005, 60)]
    )
    report = robust_validation_report(matrix, selected_config=0)
    assert report.status == "complete"
    assert report.deflated_sharpe is not None
    assert report.pbo is not None
    assert report.cpcv_paths == 15


def test_fee_sensitivity_and_untouched_window_are_explicit():
    trades = [
        {
            "symbol": "BTCUSD",
            "exit_ts": (NOW + timedelta(minutes=index)).isoformat(),
            "gross_bps": 10.0,
            "net_bps": 4.64,
            "hold_seconds": 600,
            "entry_is_maker": True,
        }
        for index in range(10)
    ]
    sensitivity = fee_sensitivity(trades, slippage_bps_per_leg=1.5)
    assert len(sensitivity) == 4
    best = max(sensitivity, key=lambda row: row["net_bps"])
    assert best["scalper_opted_in"] and best["deto_enabled"]
    assert best["fixed_trade_set_only"]
    untouched = untouched_window_summary(trades)
    assert untouched["selection_window"]["trades"] == 8
    assert untouched["second_untouched_window"]["trades"] == 2
