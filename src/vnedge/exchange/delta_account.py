"""Delta-native read-only account truth for the live risk gateway."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vnedge.exchange.delta_contracts import (
    DeltaContractSpec,
    base_quantity_from_contracts,
    notional_usd_from_contracts,
)
from vnedge.execution.order_manager import FlattenTarget
from vnedge.risk.risk_manager import AccountState


class DeltaReadOnlyAccountProvider:
    def __init__(
        self,
        *,
        safety_client,
        product_ids: Mapping[str, int],
        contract_specs: Mapping[str, DeltaContractSpec],
        base_currency: str,
        equity_ledger_path: str | Path = "data/delta_daily_equity.json",
        loss_streak_ledger_path: str | Path = "data/delta_loss_streak.json",
    ) -> None:
        if not product_ids:
            raise ValueError("Delta account provider requires product ids")
        self._client = safety_client
        self._product_ids = dict(product_ids)
        self._contract_specs = dict(contract_specs)
        if set(self._contract_specs) != set(self._product_ids):
            raise ValueError("Delta account provider specs must match product ids")
        self._symbols_by_id = {value: key for key, value in self._product_ids.items()}
        self.base_currency = base_currency.upper()
        if not self.base_currency:
            raise ValueError("Delta account base currency is required")
        self._equity_ledger_path = Path(equity_ledger_path)
        self._loss_streak_ledger_path = Path(loss_streak_ledger_path)

    async def _truth(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        import asyncio

        positions = [
            dict(await asyncio.to_thread(self._client.get_position, product_id))
            for product_id in self._product_ids.values()
        ]
        wallets = [
            dict(row)
            for row in await asyncio.to_thread(self._client.get_wallet_balances)
        ]
        return positions, wallets

    async def account_state(self) -> AccountState:
        positions, wallets = await self._truth()
        equity = _wallet_equity(wallets, self.base_currency)
        baseline, peak = self._daily_equity_state(equity)
        open_positions = [row for row in positions if _position_size(row) != 0]
        consecutive_losses = self._loss_streak_state(equity, open_positions)
        exposure: dict[str, float] = {}
        for row in open_positions:
            symbol = self._symbols_by_id.get(int(row.get("product_id") or 0), "")
            entry = float(row.get("entry_price") or 0)
            spec = self._contract_specs.get(symbol)
            if spec is None or entry <= 0:
                raise RuntimeError(f"Delta position cannot be valued safely: {row}")
            exposure[symbol] = notional_usd_from_contracts(
                contracts=abs(int(_position_size(row))),
                entry_price=entry,
                spec=spec,
            )
        return AccountState(
            equity_usd=equity,
            daily_pnl_usd=equity - baseline,
            peak_equity_usd=peak,
            open_positions=len(open_positions),
            exposure_by_symbol_usd=exposure,
            total_exposure_usd=sum(exposure.values()),
            consecutive_losses=consecutive_losses,
        )

    async def open_positions(self) -> list[FlattenTarget]:
        positions, _wallets = await self._truth()
        result: list[FlattenTarget] = []
        for row in positions:
            size = _position_size(row)
            if size == 0:
                continue
            product_id = int(row.get("product_id") or 0)
            symbol = self._symbols_by_id.get(product_id, "")
            if not symbol:
                raise RuntimeError(f"Delta position has unknown product identity: {row}")
            entry = float(row.get("entry_price") or 0)
            spec = self._contract_specs.get(symbol)
            if spec is None or entry <= 0:
                raise RuntimeError(f"Delta position cannot be flattened safely: {row}")
            result.append(
                FlattenTarget(
                    symbol=symbol,
                    side="long" if size > 0 else "short",
                    quantity=base_quantity_from_contracts(
                        contracts=abs(int(size)),
                        entry_price=entry,
                        spec=spec,
                    ),
                )
            )
        return result

    async def close(self) -> None:
        # Transport ownership belongs to the execution adapter.
        return None

    def _daily_equity_state(self, equity: float) -> tuple[float, float]:
        """Durable UTC-day baseline; failure blocks account truth.

        Equity delta is conservative: fees and unrealized/realized loss all
        count against the daily stop.  Deposits can only make the limit more
        conservative after an operator review; this code never resets it
        intraday.
        """

        today = datetime.now(UTC).date().isoformat()
        path = self._equity_ledger_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = json.loads(path.read_text()) if path.exists() else {}
            if existing.get("utc_date") == today:
                baseline = float(existing["baseline_equity"])
                peak = max(float(existing.get("peak_equity", baseline)), equity)
            else:
                baseline = peak = equity
            payload = {
                "utc_date": today,
                "baseline_equity": baseline,
                "peak_equity": peak,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            temporary = path.with_suffix(path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            return baseline, peak
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError("Delta daily equity ledger is unavailable") from exc

    def _loss_streak_state(
        self,
        equity: float,
        open_positions: Sequence[Mapping[str, Any]],
    ) -> int:
        """Persist a conservative completed-round-trip loss streak.

        The Delta position endpoint is authoritative for flat/non-flat state.
        When a product first becomes non-flat we snapshot total wallet equity;
        when it becomes flat we compare equity after all fees and funding.  The
        production service owns one symbol, so this avoids inventing realized
        PnL from candle prices while still enforcing the consecutive-loss stop.
        """

        path = self._loss_streak_ledger_path
        current_symbols: set[str] = set()
        for row in open_positions:
            symbol = self._symbols_by_id.get(int(row.get("product_id") or 0), "")
            if not symbol:
                raise RuntimeError(f"Delta position has unknown product identity: {row}")
            current_symbols.add(symbol)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = json.loads(path.read_text()) if path.exists() else {}
            streak = int(existing.get("consecutive_losses", 0))
            if streak < 0:
                raise ValueError("negative loss streak")
            open_equity = {
                str(symbol): float(value)
                for symbol, value in dict(existing.get("open_equity", {})).items()
            }
            for symbol in sorted(current_symbols - open_equity.keys()):
                open_equity[symbol] = equity
            for symbol in sorted(open_equity.keys() - current_symbols):
                net_change = equity - open_equity.pop(symbol)
                streak = streak + 1 if net_change < 0 else 0
            payload = {
                "consecutive_losses": streak,
                "open_equity": open_equity,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            _atomic_json_write(path, payload)
            return streak
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError("Delta loss-streak ledger is unavailable") from exc


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _position_size(row: Mapping[str, Any]) -> float:
    return float(row.get("size", row.get("position_size", 0)) or 0)


def _wallet_equity(rows: Sequence[Mapping[str, Any]], currency: str) -> float:
    for row in rows:
        asset = str(
            row.get("asset_symbol")
            or row.get("symbol")
            or row.get("currency")
            or ""
        ).upper()
        if asset != currency:
            continue
        for field in ("balance", "available_balance", "wallet_balance"):
            value = row.get(field)
            if value is not None:
                return float(value)
    raise RuntimeError(f"Delta wallet truth has no {currency} equity balance")
