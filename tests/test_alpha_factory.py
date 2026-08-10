"""Research-only structural alpha factory."""

from datetime import UTC, datetime, timedelta

import pandas as pd

from vnedge.research.alpha_factory import (
    AlphaFactoryConfig,
    build_alpha_tournament,
    mine_structural_alpha_events,
    run_alpha_factory,
)
from vnedge.research.universe import ResearchTarget
from vnedge.scalping.microstructure import TopOfBook, TradeTick


SYM = "BTC/USDT:USDT"
T0 = datetime(2026, 7, 5, tzinfo=UTC)


def book(ms: int, mid: float, *, bid_size: float = 10.0,
         ask_size: float = 2.0) -> tuple[int, str, TopOfBook]:
    ts = T0 + timedelta(milliseconds=ms)
    return (
        ms,
        "book",
        TopOfBook(
            symbol=SYM,
            bid=mid - 0.01,
            bid_size=bid_size,
            ask=mid + 0.01,
            ask_size=ask_size,
            event_time=ts,
        ),
    )


def trade(ms: int, price: float, side: str = "buy") -> tuple[int, str, TradeTick]:
    ts = T0 + timedelta(milliseconds=ms)
    return (
        ms,
        "trade",
        TradeTick(symbol=SYM, price=price, quantity=1.0,
                  taker_side=side, event_time=ts),
    )


def rising_pressure_events() -> list[tuple[int, str, object]]:
    events: list[tuple[int, str, object]] = []
    for i in range(30):
        ms = i * 100
        mid = 100.0 + i * 0.04
        events.append(trade(ms, mid + 0.01, "buy"))
        events.append(book(ms + 1, mid))
    return events


def flat_pressure_events() -> list[tuple[int, str, object]]:
    events: list[tuple[int, str, object]] = []
    for i in range(30):
        ms = i * 100
        events.append(trade(ms, 100.01, "buy"))
        events.append(book(ms + 1, 100.0))
    return events


def epoch_pressure_events() -> list[tuple[int, str, object]]:
    base = int(T0.timestamp() * 1000)
    events: list[tuple[int, str, object]] = []
    for i in range(30):
        ts_ms = base + i * 100
        mid = 100.0 + i * 0.04
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        events.append((
            ts_ms,
            "trade",
            TradeTick(symbol=SYM, price=mid + 0.01, quantity=1.0,
                      taker_side="buy", event_time=ts),
        ))
        events.append((
            ts_ms + 1,
            "book",
            TopOfBook(
                symbol=SYM,
                bid=mid - 0.01,
                bid_size=10.0,
                ask=mid + 0.01,
                ask_size=2.0,
                event_time=ts + timedelta(milliseconds=1),
            ),
        ))
    return events


def bullish_context() -> dict[str, pd.DataFrame]:
    base = T0 - timedelta(hours=24)
    closes = [100, 101, 102, 103, 104, 105, 106]
    freqs = {"4h": "4h", "1h": "1h", "15m": "15min", "1m": "1min"}
    out: dict[str, pd.DataFrame] = {}
    for timeframe, freq in freqs.items():
        timestamps = pd.date_range(base, periods=len(closes), freq=freq)
        out[timeframe] = pd.DataFrame({
            "timestamp": timestamps,
            "open": closes,
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        })
    return out


def config(**kwargs) -> AlphaFactoryConfig:
    defaults = dict(
        horizons_ms=(200,),
        sample_every_ms=0,
        min_samples=5,
        max_spread_bps=3.0,
        min_trade_count=1,
        min_pressure_notional_usd=1.0,
        maker_bps=0.0,
        taker_bps=0.0,
        slippage_bps=0.0,
        safety_buffer_bps=0.0,
    )
    defaults.update(kwargs)
    return AlphaFactoryConfig(**defaults)


def test_structural_alpha_routes_to_replay_but_never_trades():
    results = mine_structural_alpha_events(
        rising_pressure_events(),
        exchange="binanceusdm",
        symbol=SYM,
        day="20260705",
        config=config(),
    )

    assert results
    best = results[0]
    assert best.state in {"REPLAY_REQUIRED_MAKER", "REPLAY_REQUIRED_TAKER"}
    assert best.avg_net_bps and best.avg_net_bps > 0
    assert best.route_decision.route in {"MAKER_ONLY", "TAKER_ALLOWED"}
    assert best.can_trade is False
    assert best.can_promote is False
    assert best.execution_evidence == "hypothesis_only"
    assert best.fill_assumption == "synthetic_observation_fill_not_replay"
    assert best.requires_conservative_replay is True
    assert best.requires_untouched_judgment is True

    tournament = build_alpha_tournament(results)
    assert tournament["tournament_id"] == "event_scalper_alpha_tournament_v1"
    assert tournament["can_trade"] is False
    assert tournament["can_promote"] is False
    assert tournament["summary"]["replay_queue"] > 0
    top = tournament["standings"][0]
    assert top["decision"] in {"REPLAY_MAKER_CANDIDATE", "REPLAY_TAKER_CANDIDATE"}
    assert top["family"] in tournament["policy"]["active_research_families"]
    assert top["route_gap"]["maker_net_gap_bps"] is not None
    assert tournament["replay_queue"][0]["requires_human_approval"] is True
    assert tournament["policy"]["tombstoned_families"][0]["family_id"] == (
        "book_imbalance_continuation"
    )


def test_structural_alpha_below_cost_stays_blocked():
    results = mine_structural_alpha_events(
        flat_pressure_events(),
        exchange="binanceusdm",
        symbol=SYM,
        day="20260705",
        config=config(maker_bps=2.0, taker_bps=5.0,
                      slippage_bps=1.0, safety_buffer_bps=1.0),
    )

    assert results
    assert results[0].state == "BELOW_COST"
    assert results[0].route_decision.route == "BLOCKED"
    assert results[0].can_trade is False

    tournament = build_alpha_tournament(results)
    assert tournament["summary"]["replay_queue"] == 0
    assert tournament["replay_queue"] == []
    assert tournament["standings"][0]["decision"] == "BLOCKED_FEE_WALL"
    assert tournament["standings"][0]["route_gap"]["maker_net_gap_bps"] < 0


def test_structural_alpha_mines_context_tagged_lanes():
    results = mine_structural_alpha_events(
        epoch_pressure_events(),
        exchange="binanceusdm",
        symbol=SYM,
        day="20260705",
        config=config(context_enabled=True),
        context_candles=bullish_context(),
    )

    assert results
    aligned = [r for r in results if r.context_tag == "aligned"]
    assert aligned
    best = aligned[0]
    assert "|context=aligned" in best.hypothesis_id
    assert best.context_score and best.context_score > 0
    assert best.context_summary is not None
    assert best.context_summary["coverage"] == 4


def test_run_alpha_factory_without_tape_requests_recording(tmp_path):
    targets = (ResearchTarget("binanceusdm", SYM),)
    payload = run_alpha_factory(tmp_path, targets, days=())

    assert payload["hypotheses"] == []
    assert payload["tournament"]["summary"]["lanes"] == 0
    assert payload["tournament"]["summary"]["can_trade"] is False
    assert payload["replay_queue"] == []
    assert payload["flow_guards"]["raw_hypothesis_is_not_signal"] is True
    assert payload["flow_guards"]["can_trade"] is False
    assert payload["context_mining"]["timeframes"] == ["4h", "1h", "15m", "1m"]
    assert payload["recorder_directives"][0]["reason"] == "no recorded tick/L2 day available"
