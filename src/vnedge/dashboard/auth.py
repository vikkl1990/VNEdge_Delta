"""Per-user dashboard auth: named bearer tokens with roles and expiry.

Replaces the single shared ``DASHBOARD_TOKEN`` with a token *store*:

- ``DASHBOARD_USERS`` env: ``name:token:role[:expiry_iso]`` entries joined
  by ``;`` (the expiry field may itself contain ``:``  — ISO-8601 datetimes
  do — so it is always parsed as "everything after the third colon").
- ``DASHBOARD_TOKEN`` env (back-compat): still accepted, as the
  ``operator`` user with no expiry, so existing deploys keep working
  without any env change.

Roles are ``viewer``, ``operator``, and ``auditor``, mapped to permissions by
``PERMISSIONS`` / :func:`has_permission`. Every current route is read-only
(``view``), so the map gates nothing yet — it exists so a future control
surface (live-gate flip, promotion, kill-switch) enforces without another auth
migration. Stored tokens may be plaintext (legacy) or a salted hash
(``vnedge-sha256$…``, see :func:`hash_token`) so the deployed config need not
hold a usable secret.

Security invariants:
- token comparison is constant-time per stored token, and every stored
  token is compared on every attempt (no early exit on match), so timing
  does not reveal which entry matched;
- token values are never logged and never echoed in responses; auth events
  carry the user name and role only;
- malformed ``DASHBOARD_USERS`` entries are skipped LOUDLY (warning log,
  token text withheld) rather than silently ignored;
- expired tokens are rejected with an explicit reason, never treated as
  merely unknown.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ROLES: tuple[str, ...] = ("viewer", "operator", "auditor")
#: Identity assigned to the legacy shared DASHBOARD_TOKEN.
LEGACY_USER_NAME = "operator"

# --------------------------------------------------------------------------- #
# RBAC — a role→permission map. The dashboard is read-only TODAY (zero control
# routes), so `has_permission` gates nothing yet; the point is that when a
# control surface lands (live-gate flip, promotion, kill-switch reset) the
# enforcement primitive already exists and is tested, with no second auth
# migration. Roles: viewer (read-only), operator (read + every control),
# auditor (read + the audit trail, but NO control).
# --------------------------------------------------------------------------- #
PERM_VIEW = "view"  # read dashboards, lanes, journals, research
PERM_VIEW_AUDIT = "view_audit"  # read the operator-action audit trail
PERM_PROMOTE = "promote"  # promote a strategy up the mode ladder
PERM_FLIP_LIVE_GATE = "flip_live_gate"  # toggle a live_* mode / live gate
PERM_KILL_SWITCH = "kill_switch"  # trip / reset the kill switch

PERMISSIONS: dict[str, frozenset[str]] = {
    "viewer": frozenset({PERM_VIEW}),
    "auditor": frozenset({PERM_VIEW, PERM_VIEW_AUDIT}),
    "operator": frozenset(
        {PERM_VIEW, PERM_VIEW_AUDIT, PERM_PROMOTE, PERM_FLIP_LIVE_GATE, PERM_KILL_SWITCH}
    ),
}


def has_permission(role: str | None, permission: str) -> bool:
    """True iff ``role`` grants ``permission``. Unknown role → no permission."""
    return permission in PERMISSIONS.get(role or "", frozenset())


def permissions_for(role: str | None) -> list[str]:
    """Sorted permission list for a role — for /whoami and identity headers."""
    return sorted(PERMISSIONS.get(role or "", frozenset()))


# --------------------------------------------------------------------------- #
# Token hashing — so the deployed config (docker-compose env, .env on the VPS)
# can hold a NON-reversible hash instead of a usable bearer secret. The operator
# keeps the raw token in their password manager and pastes only the hash into
# the env; a leaked config no longer leaks a working token. A stored token that
# starts with TOKEN_HASH_PREFIX is treated as a hash; anything else stays a
# plaintext token (back-compat), so existing deploys are byte-for-byte
# unaffected. Tokens are high-entropy secrets, so a salted SHA-256 is
# sufficient (a slow password KDF would buy nothing against a 32-byte random
# token and only slow every request).
# --------------------------------------------------------------------------- #
TOKEN_HASH_PREFIX = "vnedge-sha256$"
_SALT_BYTES = 16


def hash_token(raw: str, *, salt: bytes | None = None) -> str:
    """Encode ``raw`` as ``vnedge-sha256$<salt_hex>$<digest_hex>`` for the env."""
    if salt is None:
        salt = os.urandom(_SALT_BYTES)
    digest = hashlib.sha256(salt + raw.encode("utf-8")).hexdigest()
    return f"{TOKEN_HASH_PREFIX}{salt.hex()}${digest}"


def is_hashed(stored: str) -> bool:
    return stored.startswith(TOKEN_HASH_PREFIX)


def verify_token(candidate: str, stored: str) -> bool:
    """Constant-time check of a candidate against a stored hash OR plaintext."""
    if not is_hashed(stored):
        return hmac.compare_digest((candidate or "").encode("utf-8"), stored.encode("utf-8"))
    try:
        salt_hex, digest_hex = stored[len(TOKEN_HASH_PREFIX):].split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        logger.warning("stored token hash is malformed — refusing to authenticate it")
        return False
    computed = hashlib.sha256(salt + (candidate or "").encode("utf-8")).hexdigest()
    return hmac.compare_digest(computed, digest_hex)


@dataclass(frozen=True)
class DashboardUser:
    """One authorized dashboard identity. ``token`` is a bearer secret:
    it must never be logged or serialized into any response."""

    name: str
    token: str  # plaintext OR a salted "vnedge-sha256$..." hash (see hash_token)
    role: str  # "viewer" | "operator" | "auditor"
    expires_at: datetime | None = None  # None = no expiry (tz-aware otherwise)


@dataclass(frozen=True)
class AuthResult:
    """Outcome of one authentication attempt. Carries identity (never the
    token) so routes can attach ``X-Dashboard-User`` and log auth events."""

    authorized: bool
    name: str | None = None
    role: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None  # populated on rejection, safe to echo in a 401


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_users_env(raw: str) -> list[DashboardUser]:
    """Parse ``DASHBOARD_USERS`` (``name:token:role[:expiry_iso];...``).

    Defensive by design: a malformed entry never takes the dashboard down
    and never poisons its neighbours — it is skipped with a WARNING that
    names the entry position (and user name when parseable) but never the
    token text.
    """
    users: list[DashboardUser] = []
    seen_names: set[str] = set()
    for idx, chunk in enumerate(raw.split(";")):
        entry = chunk.strip()
        if not entry:
            continue
        parts = entry.split(":", 3)  # expiry keeps its own colons intact
        if len(parts) < 3:
            logger.warning(
                "DASHBOARD_USERS entry %d skipped: expected name:token:role[:expiry_iso]", idx
            )
            continue
        name = parts[0].strip()
        token = parts[1].strip()
        role = parts[2].strip().lower()
        expiry_raw = parts[3].strip() if len(parts) == 4 else ""
        if not name or not token:
            logger.warning("DASHBOARD_USERS entry %d skipped: empty name or token", idx)
            continue
        if role not in ROLES:
            logger.warning(
                "DASHBOARD_USERS entry %d (%r) skipped: unknown role %r (expected %s)",
                idx, name, role, "|".join(ROLES),
            )
            continue
        expires_at: datetime | None = None
        if expiry_raw:
            try:
                expires_at = datetime.fromisoformat(expiry_raw)
            except ValueError:
                logger.warning(
                    "DASHBOARD_USERS entry %d (%r) skipped: unparseable expiry %r "
                    "(expected ISO-8601, e.g. 2026-08-01T00:00:00+00:00)",
                    idx, name, expiry_raw,
                )
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)  # naive = UTC
        if name in seen_names:
            logger.warning(
                "DASHBOARD_USERS entry %d skipped: duplicate user name %r", idx, name
            )
            continue
        seen_names.add(name)
        users.append(DashboardUser(name=name, token=token, role=role, expires_at=expires_at))
    return users


class TokenStore:
    """Immutable set of authorized dashboard users.

    ``authenticate`` is the only way in: it compares the candidate against
    EVERY stored token with :func:`hmac.compare_digest` (constant-time per
    token, no early exit) and enforces expiry on the matched entry.
    """

    def __init__(self, users: Sequence[DashboardUser] = ()) -> None:
        self._users: tuple[DashboardUser, ...] = tuple(users)

    def __len__(self) -> int:
        return len(self._users)

    @property
    def users(self) -> tuple[DashboardUser, ...]:
        return self._users

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TokenStore:
        """Load ``DASHBOARD_USERS`` plus the back-compat ``DASHBOARD_TOKEN``
        (mapped to the ``operator`` user, role=operator, no expiry)."""
        source = os.environ if env is None else env
        users = parse_users_env(source.get("DASHBOARD_USERS", ""))
        legacy = (source.get("DASHBOARD_TOKEN") or "").strip()
        if legacy:
            users.append(
                DashboardUser(name=LEGACY_USER_NAME, token=legacy, role="operator")
            )
        return cls(users)

    def authenticate(self, candidate: str, now: datetime | None = None) -> AuthResult:
        moment = now if now is not None else _utcnow()
        matched: DashboardUser | None = None
        for user in self._users:
            # Compare every token; keep the first match without breaking out
            # so the loop's timing is independent of match position. verify_token
            # handles both hashed (vnedge-sha256$...) and plaintext stored tokens
            # constant-time, so a hashed store and a legacy plaintext one behave
            # identically here.
            if verify_token(candidate, user.token):
                if matched is None:
                    matched = user
        if matched is None:
            return AuthResult(authorized=False, reason="missing or invalid token")
        if matched.expires_at is not None and moment >= matched.expires_at:
            logger.warning(
                "dashboard auth rejected: user=%s role=%s token expired at %s",
                matched.name, matched.role, matched.expires_at.isoformat(),
            )
            return AuthResult(
                authorized=False,
                name=matched.name,
                role=matched.role,
                expires_at=matched.expires_at,
                reason=f"token expired at {matched.expires_at.isoformat()}",
            )
        logger.info("dashboard auth accepted: user=%s role=%s", matched.name, matched.role)
        return AuthResult(
            authorized=True,
            name=matched.name,
            role=matched.role,
            expires_at=matched.expires_at,
        )


def _main(argv: list[str] | None = None) -> int:
    """`python -m vnedge.dashboard.auth hash` — turn a raw token into an env hash.

    The token is read from a prompt / stdin, NEVER from argv, so it never lands
    in shell history or `ps` output. Paste the printed value into DASHBOARD_USERS
    (as the token field) or DASHBOARD_TOKEN; keep the raw token in your password
    manager. The raw token then never lives in the deployed config.
    """
    import argparse
    import getpass
    import sys

    parser = argparse.ArgumentParser(prog="vnedge.dashboard.auth")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hash", help="hash a bearer token read from stdin/prompt")
    args = parser.parse_args(argv)

    if args.cmd == "hash":
        raw = (
            getpass.getpass("token: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
        )
        if not raw:
            print("no token provided", file=sys.stderr)
            return 2
        print(hash_token(raw))
        return 0
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
