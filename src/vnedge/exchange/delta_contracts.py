"""Delta India contract sizing helpers.

VNEDGE order intents express quantity in base-asset units. Delta's native
order API accepts integer contract counts, so every Delta path needs an
explicit conversion at the venue boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from urllib.request import Request, urlopen
import json


INDIA_PRODUCTS_URL = "https://api.india.delta.exchange/v2/products"
VNEDGE_HIGH_LEVERAGE_THRESHOLD = 10.0
VNEDGE_MAX_LEVERAGE = 30.0


@dataclass(frozen=True)
class DeltaContractSpec:
    symbol: str
    product_id: int | None = None
    contract_value: float = 1.0
    contract_unit_currency: str = ""
    tick_size: float | None = None
    initial_margin_pct: float | None = None
    maintenance_margin_pct: float | None = None
    min_contracts: int = 1
    contract_step: int = 1

    def __post_init__(self) -> None:
        if self.contract_value <= 0:
            raise ValueError("contract_value must be positive")
        if self.min_contracts <= 0:
            raise ValueError("min_contracts must be positive")
        if self.contract_step <= 0:
            raise ValueError("contract_step must be positive")

    @property
    def product_max_leverage(self) -> float | None:
        if self.initial_margin_pct is None or self.initial_margin_pct <= 0:
            return None
        return 100.0 / self.initial_margin_pct

    @classmethod
    def from_delta_product(cls, product: dict) -> "DeltaContractSpec":
        return cls(
            symbol=str(product["symbol"]),
            product_id=int(product["id"]) if product.get("id") is not None else None,
            contract_value=float(product["contract_value"]),
            contract_unit_currency=str(product.get("contract_unit_currency") or ""),
            tick_size=_maybe_float(product.get("tick_size")),
            initial_margin_pct=_maybe_float(product.get("initial_margin")),
            maintenance_margin_pct=_maybe_float(product.get("maintenance_margin")),
        )


@dataclass(frozen=True)
class DeltaSizedTrade:
    approved: bool
    contracts: int = 0
    base_quantity: float = 0.0
    notional_usd: float = 0.0
    margin_usd: float = 0.0
    effective_leverage: float = 1.0
    requested_leverage: float = 1.0
    leverage_clamped: bool = False
    liquidation_price: float | None = None
    reason: str = ""


def fetch_india_contract_spec(symbol: str) -> DeltaContractSpec:
    native_symbol = symbol.replace("/", "").replace(":USD", "")
    req = Request(
        f"{INDIA_PRODUCTS_URL}/{native_symbol}",
        headers={"User-Agent": "vnedge-contract-sizing-audit"},
    )
    with urlopen(req, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("success"):
        raise ValueError(f"Delta product lookup failed for {symbol}: {payload}")
    return DeltaContractSpec.from_delta_product(payload["result"])


def contracts_from_base_quantity(
    *,
    base_quantity: float,
    entry_price: float,
    spec: DeltaContractSpec,
) -> int:
    """Convert VNEDGE base quantity to Delta integer contracts, rounded down."""

    if base_quantity <= 0:
        return 0
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    raw_contracts = _raw_contracts_from_base_quantity(
        base_quantity=base_quantity,
        entry_price=entry_price,
        spec=spec,
    )
    stepped = math.floor(raw_contracts / spec.contract_step) * spec.contract_step
    return int(stepped)


def size_delta_risk_trade(
    *,
    account_equity_usd: float,
    risk_per_trade_pct: float,
    entry_price: float,
    stop_price: float,
    side: str,
    requested_leverage: float,
    acknowledge_high_leverage: bool,
    spec: DeltaContractSpec,
) -> DeltaSizedTrade:
    """Mirror the Pine risk/stop sizing lens, then round to Delta contracts."""

    if side not in ("long", "short"):
        return DeltaSizedTrade(approved=False, reason=f"invalid side: {side}")
    if account_equity_usd <= 0:
        return DeltaSizedTrade(approved=False, reason="account equity must be positive")
    if risk_per_trade_pct <= 0:
        return DeltaSizedTrade(approved=False, reason="risk pct must be positive")
    if entry_price <= 0 or stop_price <= 0:
        return DeltaSizedTrade(approved=False, reason="entry/stop must be positive")
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return DeltaSizedTrade(approved=False, reason="stop distance must be positive")

    risk_usd = account_equity_usd * risk_per_trade_pct / 100.0
    raw_base_qty = risk_usd / stop_distance
    contracts = contracts_from_base_quantity(
        base_quantity=raw_base_qty,
        entry_price=entry_price,
        spec=spec,
    )
    if contracts < spec.min_contracts:
        return DeltaSizedTrade(
            approved=False,
            requested_leverage=requested_leverage,
            reason=(
                f"risk-sized quantity {raw_base_qty:.8f} {spec.contract_unit_currency} "
                f"rounds below Delta minimum {spec.min_contracts} contracts"
            ),
        )

    base_qty = base_quantity_from_contracts(contracts=contracts, entry_price=entry_price, spec=spec)
    notional = notional_usd_from_contracts(contracts=contracts, entry_price=entry_price, spec=spec)
    effective_lev, lev_clamped = effective_delta_leverage(
        requested_leverage=requested_leverage,
        acknowledge_high_leverage=acknowledge_high_leverage,
        product_max_leverage=spec.product_max_leverage,
    )
    margin = notional / effective_lev
    liq = liquidation_price(
        side=side,
        entry_price=entry_price,
        leverage=effective_lev,
        maintenance_margin_pct=spec.maintenance_margin_pct,
    )
    return DeltaSizedTrade(
        approved=True,
        contracts=contracts,
        base_quantity=base_qty,
        notional_usd=notional,
        margin_usd=margin,
        effective_leverage=effective_lev,
        requested_leverage=requested_leverage,
        leverage_clamped=lev_clamped,
        liquidation_price=liq,
    )


def effective_delta_leverage(
    *,
    requested_leverage: float,
    acknowledge_high_leverage: bool,
    product_max_leverage: float | None,
) -> tuple[float, bool]:
    if requested_leverage <= 0:
        raise ValueError("requested_leverage must be positive")
    effective = min(float(requested_leverage), VNEDGE_MAX_LEVERAGE)
    if effective > VNEDGE_HIGH_LEVERAGE_THRESHOLD and not acknowledge_high_leverage:
        effective = VNEDGE_HIGH_LEVERAGE_THRESHOLD
    if product_max_leverage is not None:
        effective = min(effective, product_max_leverage)
    return effective, not math.isclose(effective, requested_leverage)


def base_quantity_from_contracts(
    *,
    contracts: int,
    entry_price: float,
    spec: DeltaContractSpec,
) -> float:
    if contracts <= 0:
        return 0.0
    if _contract_is_quote_unit(spec):
        return contracts * spec.contract_value / entry_price
    return contracts * spec.contract_value


def notional_usd_from_contracts(
    *,
    contracts: int,
    entry_price: float,
    spec: DeltaContractSpec,
) -> float:
    if contracts <= 0:
        return 0.0
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if _contract_is_quote_unit(spec):
        return contracts * spec.contract_value
    return contracts * spec.contract_value * entry_price


def liquidation_price(
    *,
    side: str,
    entry_price: float,
    leverage: float,
    maintenance_margin_pct: float | None,
) -> float | None:
    if leverage <= 0 or maintenance_margin_pct is None:
        return None
    mmr = maintenance_margin_pct / 100.0
    if side == "long":
        return entry_price * (1.0 - 1.0 / leverage + mmr)
    if side == "short":
        return entry_price * (1.0 + 1.0 / leverage - mmr)
    return None


def _raw_contracts_from_base_quantity(
    *,
    base_quantity: float,
    entry_price: float,
    spec: DeltaContractSpec,
) -> float:
    if _contract_is_quote_unit(spec):
        return base_quantity * entry_price / spec.contract_value
    return base_quantity / spec.contract_value


def _contract_is_quote_unit(spec: DeltaContractSpec) -> bool:
    return spec.contract_unit_currency.upper() in {"USD", "USDT", "USDC"}


def _maybe_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    return float(value)
