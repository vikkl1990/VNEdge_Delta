"""Idempotency: intent keys and client order ids.

Two distinct identities, per docs/DESIGN.md §2:

- **intent_key** — the deterministic identity of a trading DECISION
  (strategy, symbol, side, decision-bar timestamp). If the same decision is
  presented twice — a replayed signal, a crash-recovery re-run — the second
  presentation is a duplicate and is dropped loudly.
- **client_order_id** — the idempotency key the VENUE sees. Minted exactly
  once per accepted intent (uuid-based, no timestamp derivation), persisted
  to the journal before submission, and reused verbatim on any retry so the
  exchange collapses duplicates instead of double-booking.
"""

from __future__ import annotations

import uuid

import pandas as pd


def make_intent_key(
    strategy_id: str, symbol: str, side: str, decision_bar_ts: pd.Timestamp
) -> str:
    """Deterministic decision identity. Same decision -> same key, always."""
    return f"{strategy_id}|{symbol}|{side}|{int(decision_bar_ts.value // 1_000_000)}"


def mint_client_order_id(prefix: str = "vne") -> str:
    """Random, minted ONCE per intent. Never derived from time or signal
    values — collision-by-coincidence and divergence-on-retry are both
    failure modes we refuse."""
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class IntentRegistry:
    """In-memory duplicate-intent guard. The journal is the durable record;
    this is the fast in-session gate."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def register(self, intent_key: str, client_order_id: str) -> bool:
        """True if newly registered; False if this decision was already seen."""
        if intent_key in self._seen:
            return False
        self._seen[intent_key] = client_order_id
        return True

    def existing_order_id(self, intent_key: str) -> str | None:
        return self._seen.get(intent_key)
