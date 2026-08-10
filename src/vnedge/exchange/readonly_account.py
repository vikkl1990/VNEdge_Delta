"""Read-only account provider — real equity/positions for shadow sizing.

For "real shadow trading": use real Binance MAINNET market data (public,
keyless — already how the trial runs) but size against your REAL account
equity. That is the only thing keys add to a shadow bot.

SAFETY — this class is deliberately crippled:
- It calls ONLY read endpoints: fetch_balance, fetch_positions. There is NO
  create_order / cancel_order method anywhere in it. It CANNOT trade.
- Pair it with read-only API keys (Binance: enable "Reading" only, NOT
  trading, NOT withdrawals). Then no order can reach the exchange even if a
  bug tried — the permission doesn't exist server-side.
- Shadow execution stays on the PaperBroker/SimulatedExchange, which also
  has no exchange-submit path. Two independent walls.

Never log secrets. Keys come from env, never code.
"""

from __future__ import annotations

import logging

from vnedge.execution.order_manager import FlattenTarget

logger = logging.getLogger(__name__)


class CcxtReadOnlyAccountProvider:
    def __init__(
        self,
        exchange_id: str = "binanceusdm",
        *,
        api_key: str,
        api_secret: str,
        base_currency: str = "USDT",
        client: object | None = None,  # injectable for tests
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("read-only account provider requires API credentials")
        self.exchange_id = exchange_id
        self.base_currency = base_currency
        if client is not None:
            self._ex = client
        else:  # pragma: no cover - network client
            import ccxt.async_support as ccxt_async

            # No sandbox: real mainnet account. Keys MUST be read-only.
            self._ex = getattr(ccxt_async, exchange_id)(
                {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
            )

    async def close(self) -> None:
        await self._ex.close()

    async def fetch_equity_usd(self) -> float:
        """Real account equity (total base-currency balance). Read-only."""
        balance = await self._ex.fetch_balance()
        total = balance.get("total", {})
        equity = total.get(self.base_currency)
        if equity is None:
            raise RuntimeError(
                f"no {self.base_currency} balance found — check the account/keys"
            )
        return float(equity)

    async def open_positions(self) -> list[FlattenTarget]:
        """Real open positions as FlattenTargets. Read-only. Empty if flat."""
        try:
            raw = await self._ex.fetch_positions()
        except Exception as exc:  # noqa: BLE001 — some accounts/venues 400 when flat
            logger.warning("fetch_positions failed (treating as flat): %s", exc)
            return []
        out: list[FlattenTarget] = []
        for p in raw:
            contracts = float(p.get("contracts") or 0.0)
            if contracts == 0:
                continue
            side = p.get("side")  # "long" | "short"
            out.append(FlattenTarget(
                symbol=p.get("symbol", ""),
                side=side if side in ("long", "short") else ("long" if contracts > 0 else "short"),
                quantity=abs(contracts),
            ))
        return out
