"""Agent Gateway bearer-token auth.

This is intentionally separate from ``vnedge.dashboard.auth``:

- dashboard tokens identify human viewers/operators for read-only UI routes;
- agent tokens identify AI clients and carry scopes, allowlists, expiry,
  paper-only defaults, and rate limits.

Raw token text is never stored on the token objects. Bootstrap config may pass a
plain token through the environment, but it is hashed immediately and discarded.
Operators can also configure pre-hashed ``token_sha256`` values.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

AGENT_TOKEN_ENV = "AGENT_GATEWAY_TOKENS_JSON"
SCOPES: frozenset[str] = frozenset({"R", "B", "W_RESEARCH", "T_PAPER"})
DEFAULT_SCOPES: frozenset[str] = frozenset({"R"})


def sha256_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_expiry(raw: str | None) -> datetime | None:
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_bool(value: Any, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _as_string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, Sequence):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return tuple(out)
    return ()


@dataclass(frozen=True)
class AgentToken:
    """One configured agent credential.

    ``token_sha256`` is the only token material retained. ``token_prefix`` is
    derived from the hash and safe for audit logs.
    """

    name: str
    token_sha256: str
    scopes: frozenset[str] = DEFAULT_SCOPES
    paper_only: bool = True
    rate_limit_per_min: int = 60
    expires_at: datetime | None = None
    markets: tuple[str, ...] = ()
    lanes: tuple[str, ...] = ()

    @classmethod
    def from_secret(
        cls,
        *,
        name: str,
        token: str,
        scopes: Sequence[str] = ("R",),
        paper_only: bool = True,
        rate_limit_per_min: int = 60,
        expires_at: datetime | None = None,
        markets: Sequence[str] = (),
        lanes: Sequence[str] = (),
    ) -> AgentToken:
        return cls(
            name=name,
            token_sha256=sha256_token(token),
            scopes=frozenset(_normalize_scopes(scopes)),
            paper_only=paper_only,
            rate_limit_per_min=rate_limit_per_min,
            expires_at=expires_at,
            markets=tuple(markets),
            lanes=tuple(lanes),
        )

    @property
    def token_prefix(self) -> str:
        return self.token_sha256[:12]


@dataclass(frozen=True)
class AgentPrincipal:
    name: str
    token_prefix: str
    scopes: frozenset[str]
    paper_only: bool
    rate_limit_per_min: int
    expires_at: datetime | None
    markets: tuple[str, ...]
    lanes: tuple[str, ...]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def market_allowed(self, exchange: str, symbol: str) -> bool:
        if not self.markets:
            return True
        normalized = f"{exchange}:{symbol}"
        exchange_wildcard = f"{exchange}:*"
        return "*" in self.markets or normalized in self.markets or exchange_wildcard in self.markets

    def lane_allowed(self, lane_id: str) -> bool:
        return not self.lanes or "*" in self.lanes or lane_id in self.lanes


@dataclass(frozen=True)
class AgentAuthResult:
    authorized: bool
    principal: AgentPrincipal | None = None
    reason: str | None = None
    token_prefix: str | None = None
    name: str | None = None


def _normalize_scopes(raw_scopes: Sequence[str] | str | None) -> tuple[str, ...]:
    if raw_scopes is None:
        return tuple(DEFAULT_SCOPES)
    if isinstance(raw_scopes, str):
        candidates = [part.strip().upper() for part in raw_scopes.split(",")]
    else:
        candidates = [str(part).strip().upper() for part in raw_scopes]
    scopes = [scope for scope in candidates if scope]
    unknown = [scope for scope in scopes if scope not in SCOPES]
    if unknown:
        raise ValueError(f"unknown agent scope(s): {', '.join(sorted(set(unknown)))}")
    return tuple(scopes or DEFAULT_SCOPES)


def _token_from_entry(entry: Mapping[str, Any], idx: int) -> AgentToken | None:
    name = str(entry.get("name", "")).strip()
    if not name:
        logger.warning("agent token entry %d skipped: empty name", idx)
        return None

    token_hash = str(entry.get("token_sha256") or entry.get("token_hash") or "").strip()
    raw_token = str(entry.get("token") or "").strip()
    if token_hash and raw_token:
        logger.warning("agent token entry %d (%s) skipped: token and token_sha256 both set", idx, name)
        return None
    if raw_token:
        token_hash = sha256_token(raw_token)
    if len(token_hash) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in token_hash):
        logger.warning(
            "agent token entry %d (%s) skipped: expected token or 64-hex token_sha256",
            idx,
            name,
        )
        return None

    try:
        scopes = frozenset(_normalize_scopes(entry.get("scopes")))
        expires_at = _parse_expiry(entry.get("expires_at"))
        rate_limit = int(entry.get("rate_limit_per_min", 60))
    except (TypeError, ValueError) as exc:
        logger.warning("agent token entry %d (%s) skipped: %s", idx, name, exc)
        return None
    if rate_limit <= 0:
        logger.warning("agent token entry %d (%s) skipped: rate_limit_per_min must be positive", idx, name)
        return None

    return AgentToken(
        name=name,
        token_sha256=token_hash.lower(),
        scopes=scopes,
        paper_only=_as_bool(entry.get("paper_only"), default=True),
        rate_limit_per_min=rate_limit,
        expires_at=expires_at,
        markets=_as_string_tuple(entry.get("markets")),
        lanes=_as_string_tuple(entry.get("lanes")),
    )


def parse_agent_tokens_json(raw: str) -> list[AgentToken]:
    """Parse ``AGENT_GATEWAY_TOKENS_JSON``.

    Expected shape:

    ``[{"name":"council","token_sha256":"...","scopes":["R","B"],
    "paper_only":true,"markets":["binanceusdm:BTC/USDT:USDT"],"lanes":["*"]}]``

    ``token`` is accepted for bootstrap/dev convenience and immediately hashed;
    ``token_sha256`` is preferred for production.
    """
    stripped = raw.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        logger.warning("%s ignored: invalid JSON (%s)", AGENT_TOKEN_ENV, exc)
        return []
    if not isinstance(payload, list):
        logger.warning("%s ignored: expected a JSON list", AGENT_TOKEN_ENV)
        return []

    tokens: list[AgentToken] = []
    seen_names: set[str] = set()
    for idx, entry in enumerate(payload):
        if not isinstance(entry, dict):
            logger.warning("agent token entry %d skipped: expected object", idx)
            continue
        token = _token_from_entry(entry, idx)
        if token is None:
            continue
        if token.name in seen_names:
            logger.warning("agent token entry %d skipped: duplicate name %s", idx, token.name)
            continue
        seen_names.add(token.name)
        tokens.append(token)
    return tokens


class AgentTokenStore:
    """Immutable token set with expiry and per-token minute rate limits."""

    def __init__(self, tokens: Sequence[AgentToken] = ()) -> None:
        self._tokens: tuple[AgentToken, ...] = tuple(tokens)
        self._rate_windows: dict[str, deque[float]] = {
            token.token_sha256: deque() for token in self._tokens
        }

    def __len__(self) -> int:
        return len(self._tokens)

    @property
    def tokens(self) -> tuple[AgentToken, ...]:
        return self._tokens

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgentTokenStore:
        source = os.environ if env is None else env
        return cls(parse_agent_tokens_json(source.get(AGENT_TOKEN_ENV, "")))

    def authenticate(
        self,
        candidate: str,
        *,
        now: datetime | None = None,
        monotonic_now: float | None = None,
    ) -> AgentAuthResult:
        token_hash = sha256_token(candidate or "")
        token_prefix = token_hash[:12]
        matched: AgentToken | None = None
        for token in self._tokens:
            if hmac.compare_digest(token_hash, token.token_sha256):
                if matched is None:
                    matched = token

        if matched is None:
            return AgentAuthResult(
                authorized=False,
                reason="missing or invalid agent token",
                token_prefix=token_prefix,
            )

        moment = now if now is not None else _utcnow()
        if matched.expires_at is not None and moment >= matched.expires_at:
            logger.warning(
                "agent auth rejected: name=%s scopes=%s token expired at %s",
                matched.name,
                ",".join(sorted(matched.scopes)),
                matched.expires_at.isoformat(),
            )
            return AgentAuthResult(
                authorized=False,
                reason=f"agent token expired at {matched.expires_at.isoformat()}",
                token_prefix=matched.token_prefix,
                name=matched.name,
            )

        if not self._rate_allowed(matched, monotonic_now=monotonic_now):
            logger.warning(
                "agent auth rate-limited: name=%s limit=%d/min",
                matched.name,
                matched.rate_limit_per_min,
            )
            return AgentAuthResult(
                authorized=False,
                reason="agent token rate limit exceeded",
                token_prefix=matched.token_prefix,
                name=matched.name,
            )

        return AgentAuthResult(
            authorized=True,
            principal=AgentPrincipal(
                name=matched.name,
                token_prefix=matched.token_prefix,
                scopes=matched.scopes,
                paper_only=matched.paper_only,
                rate_limit_per_min=matched.rate_limit_per_min,
                expires_at=matched.expires_at,
                markets=matched.markets,
                lanes=matched.lanes,
            ),
        )

    def _rate_allowed(self, token: AgentToken, *, monotonic_now: float | None = None) -> bool:
        now = monotonic_now if monotonic_now is not None else time.monotonic()
        window = self._rate_windows[token.token_sha256]
        cutoff = now - 60.0
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= token.rate_limit_per_min:
            return False
        window.append(now)
        return True

