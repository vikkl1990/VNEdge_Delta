"""Auditable Delta India futures cost model.

Rates are defaults, not venue truth: live fills must ultimately use the
exchange-reported effective commission.  The defaults implement the current
standard 2 bps maker / 5 bps taker fees, 18% GST on fees, the optional 25%
DETO discount, and the opt-in Scalper Offer's zero eligible closing fee.
"""

from __future__ import annotations

from dataclasses import dataclass

from vnedge.exchange.delta_ws import delta_native_symbol


@dataclass(frozen=True)
class FeeBreakdown:
    entry_fee_bps: float
    exit_fee_bps: float
    slippage_bps: float
    total_bps: float
    scalper_eligible: bool
    scalper_window_seconds: int | None
    deto_enabled: bool

    def to_dict(self) -> dict[str, float | bool | int | None]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class DeltaFeeModel:
    deto_enabled: bool = False
    scalper_opted_in: bool = False
    maker_fee_bps_pre_tax: float = 2.0
    taker_fee_bps_pre_tax: float = 5.0
    gst_rate: float = 0.18
    deto_discount_rate: float = 0.25
    default_slippage_bps_per_leg: float = 1.5

    def __post_init__(self) -> None:
        for name in (
            "maker_fee_bps_pre_tax",
            "taker_fee_bps_pre_tax",
            "gst_rate",
            "deto_discount_rate",
            "default_slippage_bps_per_leg",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.deto_discount_rate > 1:
            raise ValueError("DETO discount cannot exceed 100%")

    @property
    def discount_factor(self) -> float:
        return 1.0 - self.deto_discount_rate if self.deto_enabled else 1.0

    @property
    def maker_bps(self) -> float:
        return self.maker_fee_bps_pre_tax * self.discount_factor * (1 + self.gst_rate)

    @property
    def taker_bps(self) -> float:
        return self.taker_fee_bps_pre_tax * self.discount_factor * (1 + self.gst_rate)

    @staticmethod
    def scalper_window_seconds(symbol: str) -> int | None:
        native = delta_native_symbol(symbol)
        if native in {"BTCUSD", "ETHUSD"}:
            return 30 * 60
        if native in {"PAXGUSD", "SLVONUSD", "XAUTUSD"}:
            return None
        return 15 * 60

    def breakdown(
        self,
        symbol: str,
        *,
        entry_is_maker: bool,
        hold_seconds: float,
        exit_is_maker: bool = False,
        slippage_bps_per_leg: float | None = None,
        scalper_opted_in: bool | None = None,
    ) -> FeeBreakdown:
        if hold_seconds < 0:
            raise ValueError("hold_seconds cannot be negative")
        slippage = (
            self.default_slippage_bps_per_leg
            if slippage_bps_per_leg is None
            else slippage_bps_per_leg
        )
        if slippage < 0:
            raise ValueError("slippage cannot be negative")
        opted_in = self.scalper_opted_in if scalper_opted_in is None else scalper_opted_in
        window = self.scalper_window_seconds(symbol)
        eligible = bool(opted_in and window is not None and hold_seconds <= window)
        entry = self.maker_bps if entry_is_maker else self.taker_bps
        exit_fee = 0.0 if eligible else (self.maker_bps if exit_is_maker else self.taker_bps)
        slip_total = 2.0 * slippage
        return FeeBreakdown(
            entry_fee_bps=entry,
            exit_fee_bps=exit_fee,
            slippage_bps=slip_total,
            total_bps=entry + exit_fee + slip_total,
            scalper_eligible=eligible,
            scalper_window_seconds=window,
            deto_enabled=self.deto_enabled,
        )

    def round_trip_cost(
        self,
        symbol: str,
        entry_is_maker: bool,
        hold_seconds: float,
        scalper_opted_in: bool | None = None,
        *,
        exit_is_maker: bool = False,
        slippage_bps_per_leg: float | None = None,
    ) -> float:
        """Return the full modeled round-trip cost as a fraction of notional."""
        return self.breakdown(
            symbol,
            entry_is_maker=entry_is_maker,
            exit_is_maker=exit_is_maker,
            hold_seconds=hold_seconds,
            slippage_bps_per_leg=slippage_bps_per_leg,
            scalper_opted_in=scalper_opted_in,
        ).total_bps / 10_000.0

    def min_edge_bps(
        self,
        symbol: str,
        entry_is_maker: bool = True,
        buffer: float = 3.0,
        *,
        hold_seconds: float = 28 * 60,
        exit_is_maker: bool = False,
    ) -> float:
        if buffer < 0:
            raise ValueError("buffer cannot be negative")
        return self.breakdown(
            symbol,
            entry_is_maker=entry_is_maker,
            exit_is_maker=exit_is_maker,
            hold_seconds=hold_seconds,
        ).total_bps + buffer
