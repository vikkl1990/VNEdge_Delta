"""Daily signal-factory operating rules.

These rules turn "find entries" into a bounded daily production line:
entries are cut off before settlement, open exposure is forced flat before
the session ends, and a lane can stop after its daily target is banked.  The
helpers are intentionally shared by backtest and live-paper so research and
operation disagree less.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator


class DailySignalFactoryConfig(BaseModel):
    """Configurable intraday discipline for paper/shadow/live-data runners."""

    model_config = {"frozen": True}

    enabled: bool = False
    session_timezone: str = "UTC"
    entry_cutoff_minute: int = Field(
        default=22 * 60 + 30,
        ge=0,
        le=1439,
        description="Local session minute after which no new entries may open.",
    )
    force_flatten_minute: int = Field(
        default=23 * 60 + 55,
        ge=0,
        le=1439,
        description="Local session minute at/after which open exposure is closed.",
    )
    max_entries_per_day: int = Field(default=3, ge=1)
    daily_profit_target_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="When >0, entries stop once daily equity PnL reaches this.",
    )
    cancel_resting_entries_at_cutoff: bool = True
    flatten_open_positions: bool = True

    @field_validator("session_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown session timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _valid_session_window(self) -> "DailySignalFactoryConfig":
        if self.entry_cutoff_minute >= self.force_flatten_minute:
            raise ValueError("entry_cutoff_minute must be before force_flatten_minute")
        return self


def session_clock(now: datetime, config: DailySignalFactoryConfig) -> datetime:
    """Return ``now`` in the configured factory timezone."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    return now.astimezone(ZoneInfo(config.session_timezone))


def session_day(now: datetime, config: DailySignalFactoryConfig) -> date:
    return session_clock(now, config).date()


def minute_of_day(now: datetime, config: DailySignalFactoryConfig) -> int:
    local = session_clock(now, config)
    return local.hour * 60 + local.minute


def should_force_flatten(now: datetime, config: DailySignalFactoryConfig) -> bool:
    if not config.enabled or not config.flatten_open_positions:
        return False
    return minute_of_day(now, config) >= config.force_flatten_minute


def entry_block_reason(
    *,
    now: datetime,
    config: DailySignalFactoryConfig,
    entries_today: int,
    daily_pnl_usd: float,
) -> str | None:
    """Return the daily-factory reason that blocks a new entry, if any."""
    if not config.enabled:
        return None
    minute = minute_of_day(now, config)
    if minute >= config.force_flatten_minute:
        return "daily_factory_flatten_window: no new entries after force-flat time"
    if minute >= config.entry_cutoff_minute:
        return "daily_factory_entry_cutoff: no new entries late in session"
    if entries_today >= config.max_entries_per_day:
        return f"daily_factory_max_entries: {entries_today}/{config.max_entries_per_day}"
    if config.daily_profit_target_usd > 0 and daily_pnl_usd >= config.daily_profit_target_usd:
        return (
            "daily_factory_target_hit: "
            f"${daily_pnl_usd:.2f}/${config.daily_profit_target_usd:.2f}"
        )
    return None
