"""QuantifiedStrategies 95-strategy pack triage for VNEDGE.

The source we have is a title inventory, not executable strategy rules.  This
module therefore treats every item as research metadata: classify the likely
mechanism, decide whether it can be rebuilt as a VNEDGE-owned crypto hypothesis,
and queue the right port family.  It never emits proprietary rules and never
grants trade or promotion permission.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import NamedTemporaryFile


QUANTIFIED_STRATEGY_LAB_ID = "quantified_strategy_lab_v1"
DEFAULT_OUT = Path("research/live_research/quantified_strategy_lab_latest.json")
DEFAULT_FEED = Path("research/live_research/quantified_strategy_lab_feed.jsonl")
REPLAY_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")
REPLAY_VENUES = ("binanceusdm", "bybit", "delta_india")
SOURCE_POLICY = "title_only_no_paid_or_proprietary_rules_copied"


@dataclass(frozen=True)
class QuantifiedStrategySeed:
    strategy_number: int
    title: str


@dataclass(frozen=True)
class QuantifiedStrategyReview:
    strategy_number: int
    title: str
    source_state: str
    mechanism: str
    asset_scope: str
    crypto_portability: str
    crypto_fit_score: int
    recommended_port: str
    next_action: str
    rationale: str
    replay_timeframes: tuple[str, ...]
    replay_venues: tuple[str, ...]
    tags: tuple[str, ...]
    can_trade: bool = False
    can_promote: bool = False
    source_policy: str = SOURCE_POLICY


@dataclass(frozen=True)
class QuantifiedPortTask:
    task_id: str
    recommended_port: str
    source_count: int
    strategy_numbers: tuple[int, ...]
    title_examples: tuple[str, ...]
    first_backtest: str
    required_work: tuple[str, ...]
    promotion_gate: tuple[str, ...]
    can_trade: bool = False
    can_promote: bool = False


STRATEGY_SEEDS: tuple[QuantifiedStrategySeed, ...] = (
    QuantifiedStrategySeed(1, "Swing Trade Nasdaq (volatility bands)"),
    QuantifiedStrategySeed(2, "IBS Swing Trade in the S&P 500"),
    QuantifiedStrategySeed(3, "Williams R% Swing Trade in Nasdaq"),
    QuantifiedStrategySeed(4, "Two Indicator Swing Trade Strategy (Nasdaq - QQQ)"),
    QuantifiedStrategySeed(5, "Double Indicator Swing Trade in Nasdaq"),
    QuantifiedStrategySeed(6, "23 Candlestick formations"),
    QuantifiedStrategySeed(7, "XLP swing trade"),
    QuantifiedStrategySeed(8, "XLP Swing Trade"),
    QuantifiedStrategySeed(9, "Overnight Edge in Nasdaq"),
    QuantifiedStrategySeed(10, "End Of Month Overnight Edge In Nasdaq"),
    QuantifiedStrategySeed(11, "Seasonal Bond Trade (TLT)"),
    QuantifiedStrategySeed(12, "Breakout Strategy (Long) In Gold (GLD)"),
    QuantifiedStrategySeed(13, "QQQ Collapse Trading Strategy"),
    QuantifiedStrategySeed(14, "Overnight Long trade in DAX-futures (FDAX)"),
    QuantifiedStrategySeed(15, "Short swing trade in TLT (bonds)"),
    QuantifiedStrategySeed(16, "Long swing trade in XLU (utilities)"),
    QuantifiedStrategySeed(17, "Low volatility swing trade in SPY (S&P 500)"),
    QuantifiedStrategySeed(18, "Overnight long trade in SPY (S&P 500)"),
    QuantifiedStrategySeed(19, "Overnight long trade in HYG (junk bonds)"),
    QuantifiedStrategySeed(20, "Overnight long trade in SPY (S&P 500)"),
    QuantifiedStrategySeed(21, "Long swing trade in QQQ (Nasdaq)"),
    QuantifiedStrategySeed(22, "Low overnight trade in QQQ/SPY (Nasdaq/SP500)"),
    QuantifiedStrategySeed(23, "Short swing trade XLP"),
    QuantifiedStrategySeed(24, "Long swing trade XLV/XLU"),
    QuantifiedStrategySeed(25, "Long and short swing trade TLT (Long-Term Treasuries)"),
    QuantifiedStrategySeed(26, "Long overnight trade in DAX (from the close until tomorrow's open)"),
    QuantifiedStrategySeed(27, "Long holiday swing trade S&P 500 (SPY)"),
    QuantifiedStrategySeed(28, "Long seasonal swing trade German bunds (FGBL)"),
    QuantifiedStrategySeed(29, "Long swing trade real estate stocks (VNQ)"),
    QuantifiedStrategySeed(30, "Long swing trade Treasury bonds (TLT)"),
    QuantifiedStrategySeed(31, "Long oversold and overnight trade in DAX-40 (FDAX)"),
    QuantifiedStrategySeed(32, "Long swing trade GLD (gold)"),
    QuantifiedStrategySeed(33, "(Bundle 1) S&P 500 Trading Strategies (SPY Bundle)"),
    QuantifiedStrategySeed(34, "(Bundle 2) Volatility Trading Strategies (SPY Bundle)"),
    QuantifiedStrategySeed(35, "(Bundle 3) Short Selling Strategies (Bundle)"),
    QuantifiedStrategySeed(36, "(Bundle 4) Seasonal Strategies (The Holiday Trading Bundle for S&P 500/SPY)"),
    QuantifiedStrategySeed(37, "(40+) Futures Strategies"),
    QuantifiedStrategySeed(38, "RSI Trading Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(39, "Stochastic Indicator Trading Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(40, "MACD (Histogram) Trading Strategy (Nasdaq 100 - QQQ)"),
    QuantifiedStrategySeed(41, "Bollinger Band Trading Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(42, "3 Trend following Strategies (S&P 500/SPY Bundle)"),
    QuantifiedStrategySeed(43, "3 Swing Trading Strategies (QQQ Bundle)"),
    QuantifiedStrategySeed(44, "MACD Indicator Trading Strategy (Nasdaq 100 - QQQ)"),
    QuantifiedStrategySeed(45, "Heikin Ashi Trading Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(46, "LL & LH (Lower Lows & Lower Highs) Trading Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(47, "Combining long and short strategies"),
    QuantifiedStrategySeed(48, "Bitcoin Trading Strategy (3 Strategies In One Bundle)"),
    QuantifiedStrategySeed(49, "Buy the Dip Trading Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(50, "Super indicator Trading Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(51, "Money Flow Index Trading Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(52, "Momentum Trading Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(53, "Short-Term Pullback Strategy For S&P 500 (SPY)"),
    QuantifiedStrategySeed(54, "6 Larry Connors Trading Strategies (S&P 500 - SPY)"),
    QuantifiedStrategySeed(55, "IBS Trading Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(56, "Coming"),
    QuantifiedStrategySeed(57, "Coppock Trading Strategy (S&P 500/SPY)"),
    QuantifiedStrategySeed(58, "200-Day Moving Average Trading Strategy (S&P 500/SPY)"),
    QuantifiedStrategySeed(59, "Triple RSI Trading Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(60, "Coming"),
    QuantifiedStrategySeed(61, "Rubber band Trading Strategy (Nasdaq 100 - QQQ)"),
    QuantifiedStrategySeed(62, "Golden Cross Trading Strategy (S&P 500/SPY)"),
    QuantifiedStrategySeed(63, "Momentum strategy for stocks, gold, and bonds"),
    QuantifiedStrategySeed(64, "Monthly momentum strategy in gold, bonds, and stocks"),
    QuantifiedStrategySeed(65, "Last Trading Day Of The Month Trading Strategy S&P 500 (SPY)"),
    QuantifiedStrategySeed(66, "Russell 2000 rebalancing strategy (IWM)"),
    QuantifiedStrategySeed(67, "ADX Trading Strategy (Nasdaq 100 - QQQ)"),
    QuantifiedStrategySeed(68, "Candlesticks Trading Strategies (Bundle - S&P 500 - SPY)"),
    QuantifiedStrategySeed(69, "Monthly (Or Weekly) Sector Rotation Trading Strategy"),
    QuantifiedStrategySeed(70, "Bollinger Bands + RSI Trading Strategy (SMH - semiconductors)"),
    QuantifiedStrategySeed(71, "MACD + RSI Trading Strategy (XLP - consumer staples)"),
    QuantifiedStrategySeed(72, "ADX + RSI Trading Strategy (Nasdaq 100 - QQQ)"),
    QuantifiedStrategySeed(73, "Coming"),
    QuantifiedStrategySeed(74, "Coming"),
    QuantifiedStrategySeed(75, "3 VIX Trading Strategies (Bundle - Nasdaq 100 - QQQ)"),
    QuantifiedStrategySeed(76, "DMI Trading Strategies (S&P 500 - SPY)"),
    QuantifiedStrategySeed(77, "Value Vs. Growth Rotation Strategy"),
    QuantifiedStrategySeed(78, "Day Trading Strategy S&P 500 (SPY)"),
    QuantifiedStrategySeed(79, "Short Strategy For Russell 2000 (IWM)"),
    QuantifiedStrategySeed(80, "End-of-Month Strategy SP500 (SPY)"),
    QuantifiedStrategySeed(81, "Turnaround Tuesday Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(82, "Turn of the Month Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(83, "Ultimate Oscillator Strategy (SMH)"),
    QuantifiedStrategySeed(84, "Double Seven Trading Strategy (S&P 500 - SPY)"),
    QuantifiedStrategySeed(85, "Overnight Strategy for Russell 2000 (IWM)"),
    QuantifiedStrategySeed(86, "Coming"),
    QuantifiedStrategySeed(87, "Example Of Combining A Trend Following And Mean Reversion Strategy (Nasdaq 100 - QQQ)"),
    QuantifiedStrategySeed(88, "Example Of Combining Seasonal Effects In S&P 500 And Bonds"),
    QuantifiedStrategySeed(89, "First Trading Day Of The Month Trading Strategy For S&P 500 (SPY)"),
    QuantifiedStrategySeed(90, "Example Of Combining A Trend Following And Mean Reversion Strategy For S&P 500 (SPY)"),
    QuantifiedStrategySeed(91, "Day Of Week Effect On Stocks (Nasdaq 100 - QQQ)"),
    QuantifiedStrategySeed(92, "Short (Tail Risk) Trading Strategy for QQQ"),
    QuantifiedStrategySeed(93, "Short Trading Strategy In Bonds (TLT)"),
    QuantifiedStrategySeed(94, "Coming"),
    QuantifiedStrategySeed(95, "24-hour (Overnight) Strategy SPY/QQQ"),
)


def review_strategy(seed: QuantifiedStrategySeed) -> QuantifiedStrategyReview:
    title = seed.title
    lower = title.lower()
    mechanism = _mechanism(lower)
    asset_scope = _asset_scope(lower)
    portability = _portability(lower, mechanism, asset_scope)
    port = _recommended_port(lower, mechanism, portability)
    score = _fit_score(lower, mechanism, asset_scope, portability)
    next_action = _next_action(portability, port)
    tags = tuple(sorted({mechanism, asset_scope, portability, port}))
    return QuantifiedStrategyReview(
        strategy_number=seed.strategy_number,
        title=title,
        source_state="TITLE_ONLY_FROM_USER_IMAGE",
        mechanism=mechanism,
        asset_scope=asset_scope,
        crypto_portability=portability,
        crypto_fit_score=score,
        recommended_port=port,
        next_action=next_action,
        rationale=_rationale(title, mechanism, asset_scope, portability, port),
        replay_timeframes=REPLAY_TIMEFRAMES if portability.startswith("PORTABLE") else (),
        replay_venues=REPLAY_VENUES if portability.startswith("PORTABLE") else (),
        tags=tags,
    )


def build_quantified_strategy_lab_payload(
    *, generated_at: datetime | None = None
) -> dict:
    generated_at = generated_at or datetime.now(UTC)
    reviews = tuple(review_strategy(seed) for seed in STRATEGY_SEEDS)
    if len(reviews) != 95:
        raise ValueError(f"expected 95 strategies, found {len(reviews)}")
    summary = _summary(reviews)
    return {
        "lab_id": QUANTIFIED_STRATEGY_LAB_ID,
        "generated_at": generated_at.isoformat(),
        "source": {
            "name": "QuantifiedStrategies 95-strategy pack",
            "source_state": "title_inventory_from_user_image",
            "strategy_count": 95,
            "source_policy": SOURCE_POLICY,
            "operator_note": (
                "Titles are inventory only. VNEDGE must locate public rules or "
                "create its own causal hypothesis before any replay/backtest."
            ),
        },
        "summary": summary,
        "strategy_reviews": [asdict(row) for row in reviews],
        "port_tasks": [asdict(row) for row in _port_tasks(reviews)],
        "fast_track": _fast_track(reviews),
        "replay_contract": {
            "timeframes": list(REPLAY_TIMEFRAMES),
            "venues": list(REPLAY_VENUES),
            "fees": "maker/taker/slippage fee wall required",
            "exit_policy": "TP1/TP2 partial + BE/trailing once PR #306 lands",
            "promotion_gate": [
                "expected net edge >25 bps after fees and slippage",
                "PF >1.5",
                "minimum 20 historical trades",
                "untouched-window judgment before paper/shadow promotion",
            ],
        },
        "can_trade": False,
        "can_promote": False,
    }


def load_quantified_strategy_lab_payload(path: Path | None = None) -> dict:
    if path is not None and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload.get("lab_id") == QUANTIFIED_STRATEGY_LAB_ID:
            return payload
    return build_quantified_strategy_lab_payload()


def publish_quantified_strategy_lab(
    *,
    out: Path = DEFAULT_OUT,
    feed: Path | None = DEFAULT_FEED,
    generated_at: datetime | None = None,
) -> dict:
    payload = build_quantified_strategy_lab_payload(generated_at=generated_at)
    out.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=out.parent, delete=False) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(out)
    if feed is not None:
        feed.parent.mkdir(parents=True, exist_ok=True)
        with feed.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": payload["generated_at"],
                "lab_id": payload["lab_id"],
                "summary": payload["summary"],
            }, sort_keys=True) + "\n")
    return payload


def _mechanism(lower: str) -> str:
    if lower == "coming":
        return "placeholder"
    if "bitcoin" in lower:
        return "crypto_native"
    if any(x in lower for x in ("overnight", "end-of-month", "end of month", "turn of the month",
                                "first trading day", "last trading day", "day of week",
                                "turnaround tuesday", "holiday", "seasonal", "monthly")):
        return "calendar_session"
    if any(x in lower for x in ("rotation", "value vs. growth", "sector")):
        return "relative_strength_rotation"
    if any(x in lower for x in ("breakout", "collapse", "volatility", "rubber band")):
        return "volatility_breakout_reversion"
    if any(x in lower for x in ("buy the dip", "pullback", "ibs", "connors", "double seven",
                                "oversold", "williams")):
        return "mean_reversion_pullback"
    if any(x in lower for x in ("rsi", "stochastic", "macd", "bollinger", "mfi",
                                "money flow", "adx", "dmi", "coppock", "ultimate oscillator")):
        return "indicator_pack"
    if any(x in lower for x in ("golden cross", "moving average", "trend following", "momentum")):
        return "trend_momentum"
    if any(x in lower for x in ("candlestick", "heikin", "lower lows", "lower highs", "ll & lh")):
        return "price_action_structure"
    if "short" in lower or "tail risk" in lower:
        return "short_risk"
    if "bundle" in lower or "combining" in lower:
        return "strategy_bundle"
    return "swing_template"


def _asset_scope(lower: str) -> str:
    if lower == "coming":
        return "none"
    if "bitcoin" in lower:
        return "crypto"
    if any(x in lower for x in ("bond", "tlt", "hyg", "bund", "fgbl", "treasury")):
        return "rates_credit"
    if "gld" in lower or " gold" in lower or "(gold" in lower:
        return "commodity_gold"
    if any(x in lower for x in ("xlp", "xlu", "xlv", "vnq", "smh", "sector", "utilities",
                                "consumer staples", "semiconductors", "real estate")):
        return "equity_sector_etf"
    if any(x in lower for x in ("spy", "s&p", "sp500", "sp 500", "qqq", "nasdaq",
                                "russell", "iwm", "dax", "fdax")):
        return "equity_index"
    if "futures" in lower:
        return "futures_cross_asset"
    return "generic"


def _portability(lower: str, mechanism: str, asset_scope: str) -> str:
    if lower == "coming":
        return "PLACEHOLDER_NO_RULES"
    if asset_scope == "crypto":
        return "PORTABLE_CRYPTO_NATIVE"
    if mechanism in {
        "indicator_pack",
        "mean_reversion_pullback",
        "trend_momentum",
        "price_action_structure",
        "volatility_breakout_reversion",
        "short_risk",
        "strategy_bundle",
    }:
        return "PORTABLE_WITH_CHANGES"
    if mechanism == "calendar_session":
        return "PORTABLE_AS_CRYPTO_SESSION_STUDY"
    if mechanism == "relative_strength_rotation":
        return "PORTABLE_AS_CRYPTO_RELATIVE_STRENGTH"
    if asset_scope in {"rates_credit", "commodity_gold", "equity_sector_etf", "equity_index"}:
        return "RESEARCH_ONLY_ASSET_SPECIFIC"
    return "PORTABLE_WITH_CHANGES"


def _recommended_port(lower: str, mechanism: str, portability: str) -> str:
    if portability == "PLACEHOLDER_NO_RULES":
        return "wait_for_release_or_public_rules"
    if "bitcoin" in lower:
        return "bitcoin_crypto_strategy_pack_v1"
    if mechanism == "calendar_session":
        return "crypto_session_calendar_miner_v1"
    if mechanism == "relative_strength_rotation":
        return "crypto_relative_strength_rotation_v1"
    if mechanism == "volatility_breakout_reversion":
        return "range_volatility_breakout_reversion_v1"
    if mechanism == "mean_reversion_pullback":
        return "pullback_reversion_pack_v1"
    if mechanism == "indicator_pack":
        return "indicator_pack_mtf_v1"
    if mechanism == "trend_momentum":
        return "trend_momentum_pack_v1"
    if mechanism == "price_action_structure":
        return "price_action_structure_pack_v1"
    if mechanism == "short_risk":
        return "short_tail_risk_pack_v1"
    if mechanism == "strategy_bundle":
        return "ensemble_blend_lab_v1"
    return "swing_template_crypto_rebuild_v1"


def _fit_score(lower: str, mechanism: str, asset_scope: str, portability: str) -> int:
    score = 45
    if portability == "PORTABLE_CRYPTO_NATIVE":
        score += 45
    elif portability.startswith("PORTABLE"):
        score += 25
    elif portability == "RESEARCH_ONLY_ASSET_SPECIFIC":
        score -= 10
    else:
        score -= 35
    if mechanism in {"indicator_pack", "mean_reversion_pullback", "volatility_breakout_reversion",
                     "trend_momentum", "price_action_structure"}:
        score += 12
    if mechanism in {"calendar_session", "relative_strength_rotation"}:
        score += 4
    if asset_scope in {"rates_credit", "equity_sector_etf"}:
        score -= 12
    if asset_scope == "equity_index":
        score -= 5
    if portability != "PORTABLE_CRYPTO_NATIVE" and ("bundle" in lower or "combining" in lower):
        score -= 5
    if "coming" == lower:
        score = 0
    return max(0, min(100, score))


def _next_action(portability: str, port: str) -> str:
    if portability == "PLACEHOLDER_NO_RULES":
        return "WAIT_FOR_RELEASE_OR_PUBLIC_RULES"
    if portability == "RESEARCH_ONLY_ASSET_SPECIFIC":
        return "EXTRACT_GENERIC_IDEA_ONLY_THEN_REQUIRE_OPERATOR_RULES"
    return f"CREATE_VNEDGE_HYPOTHESIS_FOR_{port.upper()}"


def _rationale(
    title: str,
    mechanism: str,
    asset_scope: str,
    portability: str,
    port: str,
) -> str:
    if portability == "PLACEHOLDER_NO_RULES":
        return "No published title yet; no research action beyond tracking."
    if portability == "PORTABLE_CRYPTO_NATIVE":
        return "Crypto-native title; route first into BTC/ETH/SOL/XRP multi-timeframe replay."
    if portability == "PORTABLE_AS_CRYPTO_SESSION_STUDY":
        return (
            "Equity overnight/calendar effect cannot be copied directly, but can be "
            "rebuilt around crypto UTC, Asia/London/NY, funding, and month-end windows."
        )
    if portability == "PORTABLE_AS_CRYPTO_RELATIVE_STRENGTH":
        return "Rotation idea can map to cross-pair relative strength, liquidity, and regime ranks."
    if portability == "RESEARCH_ONLY_ASSET_SPECIFIC":
        return (
            f"{title} is tied to {asset_scope}; keep only generic {mechanism} intent "
            f"and require a VNEDGE-owned {port} port before replay."
        )
    return f"Generic {mechanism} mechanics can be rebuilt as {port} and replayed after fees."


def _summary(reviews: tuple[QuantifiedStrategyReview, ...]) -> dict:
    portability = Counter(row.crypto_portability for row in reviews)
    mechanism = Counter(row.mechanism for row in reviews)
    ports = Counter(row.recommended_port for row in reviews)
    portable = sum(
        n for key, n in portability.items()
        if key.startswith("PORTABLE")
    )
    blocked = portability["RESEARCH_ONLY_ASSET_SPECIFIC"] + portability["PLACEHOLDER_NO_RULES"]
    return {
        "total_strategies": len(reviews),
        "portable_or_adaptable": portable,
        "blocked_or_placeholder": blocked,
        "source_backed_rules": 0,
        "title_only": len(reviews),
        "can_trade": False,
        "can_promote": False,
        "by_portability": dict(sorted(portability.items())),
        "by_mechanism": dict(sorted(mechanism.items())),
        "top_port_families": dict(ports.most_common(10)),
        "highest_fit": [
            asdict(row) for row in sorted(
                reviews,
                key=lambda r: (-r.crypto_fit_score, r.strategy_number),
            )[:12]
        ],
    }


def _port_tasks(reviews: tuple[QuantifiedStrategyReview, ...]) -> tuple[QuantifiedPortTask, ...]:
    groups: dict[str, list[QuantifiedStrategyReview]] = defaultdict(list)
    for row in reviews:
        if row.crypto_portability == "PLACEHOLDER_NO_RULES":
            continue
        groups[row.recommended_port].append(row)
    tasks: list[QuantifiedPortTask] = []
    for port, rows in groups.items():
        ordered = sorted(rows, key=lambda r: (-r.crypto_fit_score, r.strategy_number))
        tasks.append(
            QuantifiedPortTask(
                task_id=f"quantified_{port}",
                recommended_port=port,
                source_count=len(rows),
                strategy_numbers=tuple(row.strategy_number for row in ordered),
                title_examples=tuple(row.title for row in ordered[:5]),
                first_backtest=_first_backtest(port),
                required_work=(
                    "confirm public rules or write VNEDGE-owned causal approximation",
                    "run 1m/5m/15m/1h/4h replay on Binance, Bybit, Delta India where data exists",
                    "compare classic TP3 exit vs active TP1/BE/trailing capture",
                ),
                promotion_gate=(
                    "expected net edge >25 bps after fees and slippage",
                    "PF >1.5",
                    "minimum 20 historical trades",
                    "untouched-window judgment required before paper/shadow",
                ),
            )
        )
    return tuple(sorted(tasks, key=lambda t: (-t.source_count, t.recommended_port)))


def _first_backtest(port: str) -> str:
    if port == "bitcoin_crypto_strategy_pack_v1":
        return "BTC/USDT and ETH/USDT on 15m/1h first, then 5m if fee wall clears."
    if port == "crypto_session_calendar_miner_v1":
        return "UTC/Asia/London/NY session windows on BTC/ETH/SOL 15m/1h."
    if port == "pullback_reversion_pack_v1":
        return "BTC/ETH/SOL 5m/15m with HTF bias and active TP ladder."
    if port == "range_volatility_breakout_reversion_v1":
        return "BTC/ETH/SOL/XRP 5m/15m compression-breakout replay."
    if port == "indicator_pack_mtf_v1":
        return "Run indicator atoms as model features first; promote only after edge model lift."
    if port == "crypto_relative_strength_rotation_v1":
        return "Cross-pair 1h/4h rotation among liquid perps, then 15m trigger."
    return "Start 15m/1h, then test 5m only when cost-adjusted edge survives."


def _fast_track(reviews: tuple[QuantifiedStrategyReview, ...]) -> list[dict]:
    return [
        {
            "chunk": "A",
            "name": "crypto native + generic breakout/pullback",
            "why": "closest to VNEDGE current scanner stack and fee-aware exits",
            "strategy_numbers": [
                row.strategy_number for row in reviews
                if row.recommended_port in {
                    "bitcoin_crypto_strategy_pack_v1",
                    "range_volatility_breakout_reversion_v1",
                    "pullback_reversion_pack_v1",
                }
            ],
        },
        {
            "chunk": "B",
            "name": "indicator atoms as model features",
            "why": "RSI/MACD/BB/ADX style signals alone are weak; use as edge-model inputs",
            "strategy_numbers": [
                row.strategy_number for row in reviews
                if row.recommended_port == "indicator_pack_mtf_v1"
            ],
        },
        {
            "chunk": "C",
            "name": "crypto session/calendar remap",
            "why": "equity overnight effects do not port directly, but crypto has funding/session clocks",
            "strategy_numbers": [
                row.strategy_number for row in reviews
                if row.recommended_port == "crypto_session_calendar_miner_v1"
            ],
        },
        {
            "chunk": "D",
            "name": "relative strength and rotation",
            "why": "maps to pair universe ranking without requiring ETF sector behavior",
            "strategy_numbers": [
                row.strategy_number for row in reviews
                if row.recommended_port == "crypto_relative_strength_rotation_v1"
            ],
        },
        {
            "chunk": "Q",
            "name": "quarantine / asset-specific",
            "why": "do not pretend bonds, DAX, ETF sector effects are crypto edge without new evidence",
            "strategy_numbers": [
                row.strategy_number for row in reviews
                if row.crypto_portability in {"RESEARCH_ONLY_ASSET_SPECIFIC", "PLACEHOLDER_NO_RULES"}
            ],
        },
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish the Quantified 95-strategy lab")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED)
    parser.add_argument("--no-feed", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    payload = build_quantified_strategy_lab_payload()
    if not args.no_write:
        publish_quantified_strategy_lab(
            out=args.out,
            feed=None if args.no_feed else args.feed,
            generated_at=datetime.fromisoformat(payload["generated_at"]),
        )
    print(
        "quantified strategy lab "
        f"{payload['summary']['total_strategies']} strategies / "
        f"{payload['summary']['portable_or_adaptable']} portable-or-adaptable"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
