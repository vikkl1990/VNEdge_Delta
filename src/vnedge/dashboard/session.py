"""Short-lived session tokens — the anonymous-token account model.

The long-lived bearer token (DASHBOARD_TOKEN / DASHBOARD_USERS) is the *root*
credential. Presenting it to ``POST /auth/session`` mints a **short-lived JWT**
carrying only ``{name, role, exp}``. The browser then uses that JWT, so the
long-lived secret stops travelling on every request and a leaked session
expires on its own. No email/password/OAuth — the root token IS the account
(the legacy token maps to the ``operator`` role, i.e. the first user is admin).

Deliberately hand-rolled HS256 (no new dependency — consistent with the repo's
single-process, dependency-light stance). Security:

- only ``alg: HS256`` is accepted; ``none`` and any other alg are rejected
  (alg-confusion guard);
- signatures are compared constant-time;
- expiry is enforced; a malformed/tampered token verifies to ``None`` (the
  caller then falls back to the token store, so nothing is granted by accident);
- the signing secret comes from ``DASHBOARD_JWT_SECRET`` or, if unset, a
  per-process random secret (so sessions simply don't survive a restart — an
  acceptable, fail-safe default that never trades continuity for a weak secret).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from vnedge.dashboard.auth import AuthResult

_HEADER = {"alg": "HS256", "typ": "JWT"}
DEFAULT_TTL_SECONDS = 900  # 15 minutes


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


@dataclass(frozen=True)
class SessionToken:
    token: str
    expires_at: datetime


class SessionIssuer:
    """Mints and verifies short-lived HS256 session JWTs."""

    def __init__(self, secret: bytes, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        if not secret:
            raise ValueError("session secret must be non-empty")
        self._secret = secret
        self.ttl_seconds = ttl_seconds

    @classmethod
    def from_env(cls, env: dict | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> "SessionIssuer":
        source = os.environ if env is None else env
        configured = (source.get("DASHBOARD_JWT_SECRET") or "").strip()
        secret = configured.encode("utf-8") if configured else os.urandom(32)
        return cls(secret, ttl_seconds=ttl_seconds)

    def _sign(self, signing_input: bytes) -> str:
        return _b64url(hmac.new(self._secret, signing_input, hashlib.sha256).digest())

    def issue(self, name: str, role: str, now: datetime | None = None) -> SessionToken:
        moment = now or datetime.now(UTC)
        expires_at = moment + timedelta(seconds=self.ttl_seconds)
        payload = {
            "sub": name,
            "role": role,
            "iat": int(moment.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        signing_input = f"{_b64url(json.dumps(_HEADER).encode())}.{_b64url(json.dumps(payload).encode())}".encode()
        token = f"{signing_input.decode()}.{self._sign(signing_input)}"
        return SessionToken(token=token, expires_at=expires_at)

    def verify(self, candidate: str, now: datetime | None = None) -> AuthResult | None:
        """Return the session identity if ``candidate`` is a valid, unexpired JWT
        signed by us; otherwise ``None`` (not a session token — try the store)."""
        parts = (candidate or "").split(".")
        if len(parts) != 3:
            return None
        header_seg, payload_seg, sig_seg = parts
        try:
            header = json.loads(_b64url_decode(header_seg))
        except (ValueError, json.JSONDecodeError):
            return None
        # alg-confusion guard: accept ONLY HS256.
        if header.get("alg") != "HS256" or header.get("typ") != "JWT":
            return None
        expected = self._sign(f"{header_seg}.{payload_seg}".encode())
        if not hmac.compare_digest(expected, sig_seg):
            return None
        try:
            payload = json.loads(_b64url_decode(payload_seg))
        except (ValueError, json.JSONDecodeError):
            return None
        exp = payload.get("exp")
        moment = int((now or datetime.now(UTC)).timestamp())
        if not isinstance(exp, int) or moment >= exp:
            return AuthResult(
                authorized=False,
                name=payload.get("sub"),
                role=payload.get("role"),
                reason="session token expired",
            )
        return AuthResult(
            authorized=True,
            name=payload.get("sub"),
            role=payload.get("role"),
            expires_at=datetime.fromtimestamp(exp, UTC),
        )
