"""Entry-protection state machine — post-stop cooldown + stop-window guard.

A pure, engine-agnostic state machine consulted by ENTRY paths only. Both
execution engines (the backtester and the live paper/shadow runner) feed it
the same two events, so a protection blocks the same decisions in research
and in operations:

  - ``on_exit(reason, bar_index)`` after every position close, and
  - ``entries_allowed(bar_index)`` before every entry evaluation.

Protections (each independently OFF by default — 0 disables):

  - ``cooldown_bars_after_stop``: after a STOP exit (reasons "stop" /
    "tick_stop"), entry evaluations are blocked for that many bars starting
    with the stop bar itself. A value of 1 blocks exactly the same-bar
    re-entry that lets a still-armed condition immediately re-fire on the
    bar that just proved the trade wrong. Take-profit / max-holding /
    end-of-data exits deliberately do NOT trigger the cooldown — a winner
    closing is not evidence the entry condition went bad.
  - ``max_stops_per_window`` within ``stop_window_bars``: once that many
    stop exits land inside the trailing window, entries are blocked until
    the oldest stop ages out. Repeated stops in a short window usually mean
    the regime changed before the strategy noticed.

INVARIANTS:

  - Protections NEVER affect exits. There is no exit-blocking API here at
    all; reduce-only exits flow through the normal pipeline untouched
    (capital protection beats entry hygiene, always).
  - Defaults are OFF. Enabling any protection changes entry behavior, so it
    is a pre-registered research decision, never a mid-trial tweak
    (docs/PROTECTIONS.md).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

# Exit reasons that count as a stop for the cooldown and window guard.
# "stop" is the bar-close/backtest stop; "tick_stop" is the live runner's
# quote-granular stop between bar closes.
STOP_EXIT_REASONS: frozenset[str] = frozenset({"stop", "tick_stop"})


class ProtectionConfig(BaseModel):
    """All-off-by-default entry protections. Frozen, like every risk config."""

    model_config = {"frozen": True}

    # After a stop exit, block entry evaluations for this many bars starting
    # with the stop bar itself. 0 = off.
    cooldown_bars_after_stop: int = Field(default=0, ge=0)
    # Block entries while >= this many stop exits sit inside the trailing
    # stop_window_bars window. 0 = off. Must be enabled together with
    # stop_window_bars.
    max_stops_per_window: int = Field(default=0, ge=0)
    stop_window_bars: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _window_guard_consistent(self) -> "ProtectionConfig":
        if (self.max_stops_per_window > 0) != (self.stop_window_bars > 0):
            raise ValueError(
                "max_stops_per_window and stop_window_bars enable the stop-window "
                "guard together: set both > 0, or leave both 0 (off)"
            )
        return self

    @property
    def enabled(self) -> bool:
        return self.cooldown_bars_after_stop > 0 or self.max_stops_per_window > 0


class ProtectionState:
    """Mutable per-session/per-run state for one symbol lane.

    ``bar_index`` is any monotonically increasing integer bar counter — the
    backtester uses its loop index, the live runner the candle-frame row
    index. Only differences between indices matter.
    """

    def __init__(self, config: ProtectionConfig) -> None:
        self.config = config
        # entries blocked while bar_index < _cooldown_until
        self._cooldown_until = 0
        self._stop_bars: list[int] = []

    def on_exit(self, reason: str, bar_index: int) -> None:
        """Record a position close. Only stop exits arm any protection."""
        if reason not in STOP_EXIT_REASONS:
            return
        if self.config.cooldown_bars_after_stop > 0:
            self._cooldown_until = max(
                self._cooldown_until,
                bar_index + self.config.cooldown_bars_after_stop,
            )
        if self.config.max_stops_per_window > 0:
            self._stop_bars.append(bar_index)

    def entries_allowed(self, bar_index: int) -> tuple[bool, str | None]:
        """May an entry be EVALUATED at this bar? Returns (allowed, reason).

        The reason carries every active block, not just the first —
        rejections must be fully explainable. Exits are never consulted
        through this (or any) method.
        """
        reasons: list[str] = []
        if bar_index < self._cooldown_until:
            remaining = self._cooldown_until - bar_index
            reasons.append(f"post_exit_cooldown: {remaining} bar(s) remaining")
        cfg = self.config
        if cfg.max_stops_per_window > 0:
            cutoff = bar_index - cfg.stop_window_bars
            self._stop_bars = [b for b in self._stop_bars if b > cutoff]
            if len(self._stop_bars) >= cfg.max_stops_per_window:
                reasons.append(
                    f"stop_window_guard: {len(self._stop_bars)} stops in last "
                    f"{cfg.stop_window_bars} bars (max {cfg.max_stops_per_window})"
                )
        if reasons:
            return False, "; ".join(reasons)
        return True, None
