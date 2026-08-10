"""Research universe selection for the slow loop.

The execution roadmap is still deliberately single-exchange for V1, but the
research lab can compare venues and symbols offline. This module keeps that
universe explicit and bounded: operators choose exchanges/symbols through
environment variables, and every target is a plain exchange/symbol/timeframe
tuple used only by the research process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Mapping

DEFAULT_EXCHANGES = ("binanceusdm", "bybit", "delta_india")
DEFAULT_SYMBOLS = (
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    "DOGE/USDT:USDT",
)
DEFAULT_TIMEFRAME = "1h"
DEFAULT_DERIVATIVE_QUOTES = ("USDT", "USDC", "USD")
DELTA_INDIA_EXCHANGE = "delta_india"


@dataclass(frozen=True, order=True)
class ResearchTarget:
    exchange: str
    symbol: str
    timeframe: str = DEFAULT_TIMEFRAME

    @property
    def key(self) -> str:
        return f"{self.exchange}|{self.symbol}|{self.timeframe}"

    @property
    def label(self) -> str:
        return f"{self.exchange}:{self.symbol}:{self.timeframe}"


@dataclass(frozen=True)
class ProfitablePair:
    exchange: str
    symbol: str
    timeframe: str
    best_strategy: str
    verdict: str
    oos_net_usd: float
    oos_trades: int
    gates: str

    def to_dict(self) -> dict:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "best_strategy": self.best_strategy,
            "verdict": self.verdict,
            "oos_net_usd": self.oos_net_usd,
            "oos_trades": self.oos_trades,
            "gates": self.gates,
        }


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_suffix(exchange: str) -> str:
    return exchange.upper().replace("-", "_").replace("/", "_")


def _delta_india_symbol(symbol: str) -> str:
    if "/USDT" not in symbol:
        return symbol
    base = symbol.split("/", maxsplit=1)[0]
    return f"{base}/USD:USD"


def _symbols_for_exchange(exchange: str, symbols: tuple[str, ...]) -> tuple[str, ...]:
    if exchange == DELTA_INDIA_EXCHANGE:
        return tuple(_delta_india_symbol(symbol) for symbol in symbols)
    return symbols


def load_research_targets(
    *,
    exchanges: Iterable[str] | None = None,
    symbols: Iterable[str] | None = None,
    timeframe: str | None = None,
) -> tuple[ResearchTarget, ...]:
    """Build the target universe.

    Env controls:
    - RESEARCH_EXCHANGES=binanceusdm,bybit,delta_india
    - RESEARCH_SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT
    - RESEARCH_SYMBOLS_BYBIT=BTC/USDT:USDT,SOL/USDT:USDT
    - RESEARCH_TIMEFRAME=1h
    """
    selected_exchanges = tuple(exchanges or _split_csv(os.environ.get("RESEARCH_EXCHANGES"))
                              or DEFAULT_EXCHANGES)
    selected_symbols = tuple(symbols or _split_csv(os.environ.get("RESEARCH_SYMBOLS"))
                             or DEFAULT_SYMBOLS)
    selected_timeframe = timeframe or os.environ.get("RESEARCH_TIMEFRAME", DEFAULT_TIMEFRAME)

    targets: list[ResearchTarget] = []
    seen: set[str] = set()
    for exchange in selected_exchanges:
        per_exchange_symbols = (
            _split_csv(os.environ.get(f"RESEARCH_SYMBOLS_{_env_suffix(exchange)}"))
            or _symbols_for_exchange(exchange, selected_symbols)
        )
        for symbol in per_exchange_symbols:
            target = ResearchTarget(exchange=exchange, symbol=symbol,
                                    timeframe=selected_timeframe)
            if target.key not in seen:
                targets.append(target)
                seen.add(target.key)
    return tuple(targets)


def summarize_universe(targets: Iterable[ResearchTarget]) -> dict:
    targets = tuple(targets)
    by_exchange: dict[str, int] = {}
    for target in targets:
        by_exchange[target.exchange] = by_exchange.get(target.exchange, 0) + 1
    return {
        "targets": len(targets),
        "exchanges": sorted(by_exchange),
        "targets_by_exchange": by_exchange,
        "symbols": sorted({t.symbol for t in targets}),
        "timeframes": sorted({t.timeframe for t in targets}),
    }


def targets_from_markets(
    exchange: str,
    markets: Mapping[str, Mapping],
    *,
    timeframe: str = DEFAULT_TIMEFRAME,
    quote_assets: Iterable[str] = DEFAULT_DERIVATIVE_QUOTES,
    active_only: bool = True,
    max_symbols: int | None = None,
) -> tuple[ResearchTarget, ...]:
    """Build research targets from CCXT market metadata.

    This is intentionally derivatives-first: active linear swaps/futures only,
    excluding options and inactive/delisted contracts. It is used by scanners to
    cover "all pairs" for research data collection; it does not change V1
    execution scope.
    """
    quote_set = {q.upper() for q in quote_assets}
    out: list[ResearchTarget] = []
    seen: set[str] = set()
    for market in markets.values():
        if not _is_research_derivative_market(market, quote_set, active_only):
            continue
        symbol = str(market.get("symbol") or "")
        if not symbol:
            continue
        target = ResearchTarget(exchange=exchange, symbol=symbol, timeframe=timeframe)
        if target.key in seen:
            continue
        out.append(target)
        seen.add(target.key)
    out.sort(key=_target_discovery_sort_key)
    if max_symbols is not None:
        out = out[:max_symbols]
    return tuple(out)


async def discover_exchange_targets(
    exchange: str,
    *,
    timeframe: str = DEFAULT_TIMEFRAME,
    quote_assets: Iterable[str] = DEFAULT_DERIVATIVE_QUOTES,
    active_only: bool = True,
    max_symbols: int | None = None,
) -> tuple[ResearchTarget, ...]:
    """Discover active derivative research targets for one exchange via CCXT."""
    import ccxt.async_support as ccxt_async
    from vnedge.data.ccxt_client import create_ccxt_async_exchange, resolve_ccxt_exchange_id

    ccxt_exchange_id = resolve_ccxt_exchange_id(exchange)
    if not hasattr(ccxt_async, ccxt_exchange_id):
        raise ValueError(f"unknown CCXT exchange id: {exchange}")
    ex = create_ccxt_async_exchange(exchange)
    try:
        markets = await ex.load_markets()
    finally:
        await ex.close()
    return targets_from_markets(
        exchange,
        markets,
        timeframe=timeframe,
        quote_assets=quote_assets,
        active_only=active_only,
        max_symbols=max_symbols,
    )


async def discover_research_targets(
    exchanges: Iterable[str],
    *,
    timeframe: str = DEFAULT_TIMEFRAME,
    quote_assets: Iterable[str] = DEFAULT_DERIVATIVE_QUOTES,
    active_only: bool = True,
    max_symbols_per_exchange: int | None = None,
) -> tuple[ResearchTarget, ...]:
    """Discover active derivatives across exchanges for research-only scanners."""
    targets: list[ResearchTarget] = []
    seen: set[str] = set()
    for exchange in exchanges:
        for target in await discover_exchange_targets(
            exchange,
            timeframe=timeframe,
            quote_assets=quote_assets,
            active_only=active_only,
            max_symbols=max_symbols_per_exchange,
        ):
            if target.key not in seen:
                targets.append(target)
                seen.add(target.key)
    return tuple(sorted(targets))


def _is_research_derivative_market(
    market: Mapping,
    quote_assets: set[str],
    active_only: bool,
) -> bool:
    if active_only and market.get("active") is False:
        return False
    if market.get("option"):
        return False
    derivative = bool(
        market.get("swap")
        or market.get("future")
        or market.get("contract")
        or market.get("type") in {"swap", "future"}
    )
    if not derivative:
        return False
    # For this project the scalper lane is linear perps/futures; inverse/quanto
    # contracts add collateral and sizing mechanics we should not mix into v1.
    if market.get("linear") is False:
        return False
    quote = str(market.get("quote") or "").upper()
    settle = str(market.get("settle") or "").upper()
    return quote in quote_assets or settle in quote_assets


def _target_discovery_sort_key(target: ResearchTarget) -> tuple[int, int, str]:
    default_rank = {
        symbol: i for i, symbol in enumerate(DEFAULT_SYMBOLS)
    }
    if target.symbol in default_rank:
        return (0, default_rank[target.symbol], target.symbol)
    base = target.symbol.split("/")[0]
    base_rank = {
        symbol.split("/")[0]: i for i, symbol in enumerate(DEFAULT_SYMBOLS)
    }
    return (1, base_rank.get(base, len(DEFAULT_SYMBOLS)), target.symbol)


def profitable_pairs(
    records: Iterable[dict],
    *,
    min_oos_net_usd: float = 0.0,
    min_oos_trades: int = 10,
    include_positive_rejects: bool = True,
) -> tuple[ProfitablePair, ...]:
    """Select the best currently profitable lane per exchange/symbol.

    PASS rows are strongest. Positive REJECT rows are still useful research
    targets because they can be close to passing, but they remain candidates.
    """
    best: dict[tuple[str, str, str], dict] = {}
    for record in records:
        if record.get("auto"):
            continue
        verdict = record.get("verdict", "REJECT")
        net = float(record.get("oos_net_usd", 0.0))
        trades = int(record.get("oos_trades", 0))
        profitable = net > min_oos_net_usd and trades >= min_oos_trades
        if verdict != "PASS" and not (include_positive_rejects and profitable):
            continue
        key = (
            record.get("exchange", "binanceusdm"),
            record.get("symbol", ""),
            record.get("timeframe", DEFAULT_TIMEFRAME),
        )
        score = (2 if verdict == "PASS" else 1, net, trades)
        prev = best.get(key)
        prev_score = (
            2 if prev and prev.get("verdict") == "PASS" else 1,
            float(prev.get("oos_net_usd", 0.0)) if prev else float("-inf"),
            int(prev.get("oos_trades", 0)) if prev else -1,
        )
        if prev is None or score > prev_score:
            best[key] = record

    pairs = [
        ProfitablePair(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            best_strategy=record["strategy"],
            verdict=record.get("verdict", "REJECT"),
            oos_net_usd=round(float(record.get("oos_net_usd", 0.0)), 2),
            oos_trades=int(record.get("oos_trades", 0)),
            gates=record.get("gates", "standard"),
        )
        for (exchange, symbol, timeframe), record in best.items()
    ]
    return tuple(sorted(pairs, key=lambda p: (p.verdict != "PASS", -p.oos_net_usd,
                                             p.exchange, p.symbol)))
