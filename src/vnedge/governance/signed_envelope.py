"""Signed governance envelopes and single-use replay protection.

Hash-linked proof payloads establish deterministic integrity.  This module
adds the identity boundary: a trusted Ed25519 issuer signs the canonical proof
payload and a durable nonce store prevents the same authorization from being
used to start more than one governed runtime.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from vnedge.governance.crypto import (
    GovernanceKeyring,
    GovernanceSigner,
    generate_nonce,
    validate_nonce,
)
from vnedge.governance.proofs import PaperEligibilityProof, canonical_json
from vnedge.governance.promotion_policy import PromotionPolicy


class SignedPaperEligibilityEnvelope(BaseModel):
    """A paper proof signed by a trusted governance identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    envelope_schema_version: str = "1"
    proof: PaperEligibilityProof
    issuer_pubkey: str
    nonce: str
    signature: str

    @model_validator(mode="after")
    def validate_envelope(self) -> "SignedPaperEligibilityEnvelope":
        if self.envelope_schema_version != "1":
            raise ValueError("unsupported signed envelope schema version")
        validate_nonce(self.nonce)
        if not self.issuer_pubkey or not self.signature:
            raise ValueError("signed envelope requires public key and signature")
        return self

    def signing_payload(self) -> bytes:
        return canonical_json(
            {
                "envelope_schema_version": self.envelope_schema_version,
                "proof": self.proof.model_dump(mode="json"),
                "issuer_pubkey": self.issuer_pubkey,
                "nonce": self.nonce,
            }
        ).encode("utf-8")

    @classmethod
    def issue(
        cls,
        proof: PaperEligibilityProof,
        *,
        signer: GovernanceSigner,
        nonce: str | None = None,
    ) -> "SignedPaperEligibilityEnvelope":
        if signer.issuer != proof.issuer:
            raise ValueError("signer issuer must match paper proof issuer")
        issued_nonce = nonce or generate_nonce()
        unsigned: dict[str, Any] = {
            "envelope_schema_version": "1",
            "proof": proof,
            "issuer_pubkey": signer.issuer_pubkey,
            "nonce": issued_nonce,
            "signature": "pending",
        }
        draft = cls.model_construct(**unsigned)
        unsigned["signature"] = signer.sign(draft.signing_payload())
        return cls.model_validate(unsigned)

    def verify(
        self,
        *,
        keyring: GovernanceKeyring,
        policy: PromotionPolicy,
        strategy_id: str,
        symbol: str,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        failures = list(
            self.proof.verify(
                policy=policy,
                now=current,
                strategy_id=strategy_id,
                symbol=symbol,
            )
        )
        try:
            keyring.verify(
                payload=self.signing_payload(),
                issuer=self.proof.issuer,
                issuer_pubkey=self.issuer_pubkey,
                signature=self.signature,
                now=current,
            )
        except ValueError as exc:
            failures.append(f"governance signature invalid: {exc}")
        return tuple(failures)


class SignedStageAuthorization(BaseModel):
    """Single-use authorization for exactly one operating-mode transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    envelope_schema_version: str = "1"
    issuer: str
    issuer_pubkey: str
    strategy_id: str
    symbol: str
    source_commit: str
    config_sha256: str
    current_stage: str
    target_stage: str
    created_at: datetime
    expires_at: datetime
    nonce: str
    claims: dict[str, Any]
    signature: str

    @model_validator(mode="after")
    def validate_authorization(self) -> "SignedStageAuthorization":
        required = (
            "issuer",
            "issuer_pubkey",
            "strategy_id",
            "symbol",
            "source_commit",
            "config_sha256",
            "current_stage",
            "target_stage",
            "signature",
        )
        if any(not str(getattr(self, field)).strip() for field in required):
            raise ValueError("stage authorization fields cannot be empty")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("stage authorization timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("stage authorization must expire after creation")
        validate_nonce(self.nonce)
        return self

    def signing_payload(self) -> bytes:
        return canonical_json(
            self.model_dump(mode="json", exclude={"signature"})
        ).encode("utf-8")

    @classmethod
    def issue(
        cls,
        *,
        signer: GovernanceSigner,
        strategy_id: str,
        symbol: str,
        source_commit: str,
        config_sha256: str,
        current_stage: str,
        target_stage: str,
        claims: dict[str, Any],
        created_at: datetime | None = None,
        ttl: timedelta = timedelta(hours=12),
        nonce: str | None = None,
    ) -> "SignedStageAuthorization":
        created = (created_at or datetime.now(UTC)).astimezone(UTC)
        values: dict[str, Any] = {
            "envelope_schema_version": "1",
            "issuer": signer.issuer,
            "issuer_pubkey": signer.issuer_pubkey,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "source_commit": source_commit,
            "config_sha256": config_sha256,
            "current_stage": current_stage,
            "target_stage": target_stage,
            "created_at": created,
            "expires_at": created + ttl,
            "nonce": nonce or generate_nonce(),
            "claims": claims,
            "signature": "pending",
        }
        draft = cls.model_construct(**values)
        values["signature"] = signer.sign(draft.signing_payload())
        return cls.model_validate(values)

    def verify(
        self,
        *,
        keyring: GovernanceKeyring,
        strategy_id: str,
        symbol: str,
        source_commit: str,
        config_sha256: str,
        target_stage: str,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        failures: list[str] = []
        expected = {
            "strategy_id": (self.strategy_id, strategy_id),
            "symbol": (self.symbol, symbol),
            "source_commit": (self.source_commit, source_commit),
            "config_sha256": (self.config_sha256, config_sha256),
            "target_stage": (self.target_stage, target_stage),
        }
        for name, (actual, wanted) in expected.items():
            if actual != wanted:
                failures.append(f"{name} mismatch: {actual} != {wanted}")
        if current < self.created_at.astimezone(UTC):
            failures.append("stage authorization creation timestamp is in the future")
        if current >= self.expires_at.astimezone(UTC):
            failures.append("stage authorization expired")
        if self.claims.get("approved") is not True:
            failures.append("stage authorization is not approved")
        try:
            keyring.verify(
                payload=self.signing_payload(),
                issuer=self.issuer,
                issuer_pubkey=self.issuer_pubkey,
                signature=self.signature,
                now=current,
            )
        except ValueError as exc:
            failures.append(f"governance signature invalid: {exc}")
        return tuple(failures)


class NonceReplayStore:
    """Transactional, durable single-use nonce store.

    The unique primary key makes concurrent consumers fail closed.  A nonce is
    consumed only after signature and proof validation have succeeded.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS consumed_governance_nonces (
                    nonce TEXT PRIMARY KEY,
                    proof_hash TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    consumed_at TEXT NOT NULL
                )
                """
            )

    def consume(self, *, nonce: str, proof_hash: str, purpose: str) -> bool:
        validate_nonce(nonce)
        if not proof_hash or not purpose:
            raise ValueError("nonce consumption requires proof hash and purpose")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO consumed_governance_nonces
                        (nonce, proof_hash, purpose, consumed_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (nonce, proof_hash, purpose, datetime.now(UTC).isoformat()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def consumed(self, nonce: str) -> bool:
        validate_nonce(nonce)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM consumed_governance_nonces WHERE nonce = ?",
                (nonce,),
            ).fetchone()
        return row is not None


def load_signed_paper_envelope(path: str | Path) -> SignedPaperEligibilityEnvelope:
    try:
        payload = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"unable to read signed paper proof: {path}") from exc
    return SignedPaperEligibilityEnvelope.model_validate_json(payload)


def load_signed_stage_authorization(path: str | Path) -> SignedStageAuthorization:
    try:
        payload = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"unable to read signed stage authorization: {path}") from exc
    return SignedStageAuthorization.model_validate_json(payload)


def load_governance_keyring(path: str | Path) -> GovernanceKeyring:
    """Load a public-only keyring file.

    Format: ``{"keys": [{"issuer": ..., "issuer_pubkey": ...}]}`` with
    optional ISO ``not_before``, ``not_after`` and ``revoked`` values.
    """

    from vnedge.governance.crypto import TrustedGovernanceKey

    try:
        root = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load governance keyring: {path}") from exc
    rows = root.get("keys") if isinstance(root, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("governance keyring must contain a non-empty keys list")

    def parse_time(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            raise ValueError("governance key validity timestamps must be timezone-aware")
        return parsed.astimezone(UTC)

    records = tuple(
        TrustedGovernanceKey(
            issuer=str(row["issuer"]),
            issuer_pubkey=str(row["issuer_pubkey"]),
            not_before=parse_time(row.get("not_before")),
            not_after=parse_time(row.get("not_after")),
            revoked=bool(row.get("revoked", False)),
        )
        for row in rows
        if isinstance(row, dict)
    )
    if len(records) != len(rows):
        raise ValueError("governance keyring rows must be objects")
    return GovernanceKeyring(records)
