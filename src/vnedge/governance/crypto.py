"""Ed25519 identity primitives for governance proof envelopes.

This module deliberately does not modify promotion proofs or persist replay
nonces yet. It provides the fail-closed trust boundary those migrations will
consume: private-key loading, detached signing, trusted-key lookup, rotation,
revocation, validity windows, and detached signature verification.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ALGORITHM = "Ed25519"
KEY_ID_PREFIX = "ed25519:"
NONCE_BYTES = 32
PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64
MAX_KEY_PEM_BYTES = 64 * 1024


class GovernanceCryptoError(ValueError):
    """Base class for fail-closed governance cryptography failures."""


class GovernanceKeyLoadError(GovernanceCryptoError):
    """A key could not be loaded safely or was not an Ed25519 key."""


class InsecurePrivateKeyPermissions(GovernanceKeyLoadError):
    """A private-key file is readable or writable by group/other users."""


class UntrustedGovernanceKey(GovernanceCryptoError):
    """The supplied public key is not present in the governance keyring."""


class InactiveGovernanceKey(GovernanceCryptoError):
    """The trusted key is revoked or outside its validity interval."""


class GovernanceSignatureError(GovernanceCryptoError):
    """A detached Ed25519 signature is malformed or invalid."""


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str, *, expected_bytes: int, field_name: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise GovernanceCryptoError(f"{field_name} must be non-empty base64url")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise GovernanceCryptoError(f"{field_name} is not valid base64url") from exc
    if len(decoded) != expected_bytes:
        raise GovernanceCryptoError(
            f"{field_name} must decode to exactly {expected_bytes} bytes"
        )
    if _b64url_encode(decoded) != value:
        raise GovernanceCryptoError(f"{field_name} must use canonical unpadded base64url")
    return decoded


def _validate_pem_size(pem: bytes, *, field_name: str) -> None:
    if not isinstance(pem, bytes):
        raise TypeError(f"{field_name} must be bytes")
    if not pem or len(pem) > MAX_KEY_PEM_BYTES:
        raise GovernanceKeyLoadError(
            f"{field_name} must contain between 1 and {MAX_KEY_PEM_BYTES} bytes"
        )


def public_key_b64(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64url_encode(raw)


def public_key_id(public_key_value: str | Ed25519PublicKey) -> str:
    encoded = (
        public_key_value
        if isinstance(public_key_value, str)
        else public_key_b64(public_key_value)
    )
    raw = _b64url_decode(
        encoded,
        expected_bytes=PUBLIC_KEY_BYTES,
        field_name="issuer_pubkey",
    )
    return KEY_ID_PREFIX + hashlib.sha256(raw).hexdigest()


def generate_nonce() -> str:
    """Return a 256-bit cryptographic nonce encoded without padding."""
    return _b64url_encode(secrets.token_bytes(NONCE_BYTES))


def validate_nonce(nonce: str) -> None:
    _b64url_decode(nonce, expected_bytes=NONCE_BYTES, field_name="nonce")


def _normalize_time(value: datetime | None, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise GovernanceCryptoError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class TrustedGovernanceKey:
    """Immutable public-key record controlled by the governance trust store."""

    issuer: str
    issuer_pubkey: str
    not_before: datetime | None = None
    not_after: datetime | None = None
    revoked: bool = False

    def __post_init__(self) -> None:
        if not self.issuer or self.issuer.strip() != self.issuer:
            raise GovernanceCryptoError("issuer must be a non-empty trimmed value")
        _b64url_decode(
            self.issuer_pubkey,
            expected_bytes=PUBLIC_KEY_BYTES,
            field_name="issuer_pubkey",
        )
        start = _normalize_time(self.not_before, field_name="not_before")
        end = _normalize_time(self.not_after, field_name="not_after")
        if start is not None:
            object.__setattr__(self, "not_before", start)
        if end is not None:
            object.__setattr__(self, "not_after", end)
        if start is not None and end is not None and end <= start:
            raise GovernanceCryptoError("not_after must be later than not_before")

    @property
    def key_id(self) -> str:
        return public_key_id(self.issuer_pubkey)

    @property
    def public_key(self) -> Ed25519PublicKey:
        raw = _b64url_decode(
            self.issuer_pubkey,
            expected_bytes=PUBLIC_KEY_BYTES,
            field_name="issuer_pubkey",
        )
        return Ed25519PublicKey.from_public_bytes(raw)

    def assert_active(self, *, now: datetime | None = None) -> None:
        current = _normalize_time(now or datetime.now(UTC), field_name="now")
        assert current is not None
        if self.revoked:
            raise InactiveGovernanceKey(f"governance key is revoked: {self.key_id}")
        if self.not_before is not None and current < self.not_before:
            raise InactiveGovernanceKey(f"governance key is not active yet: {self.key_id}")
        if self.not_after is not None and current >= self.not_after:
            raise InactiveGovernanceKey(f"governance key has expired: {self.key_id}")

    @classmethod
    def from_public_key(
        cls,
        *,
        issuer: str,
        public_key: Ed25519PublicKey,
        not_before: datetime | None = None,
        not_after: datetime | None = None,
    ) -> TrustedGovernanceKey:
        return cls(
            issuer=issuer,
            issuer_pubkey=public_key_b64(public_key),
            not_before=not_before,
            not_after=not_after,
        )

    @classmethod
    def from_public_pem(
        cls,
        *,
        issuer: str,
        pem: bytes,
        not_before: datetime | None = None,
        not_after: datetime | None = None,
    ) -> TrustedGovernanceKey:
        _validate_pem_size(pem, field_name="governance public key PEM")
        try:
            loaded = serialization.load_pem_public_key(pem)
        except (TypeError, ValueError) as exc:
            raise GovernanceKeyLoadError("unable to load governance public key PEM") from exc
        if not isinstance(loaded, Ed25519PublicKey):
            raise GovernanceKeyLoadError("governance public key must use Ed25519")
        return cls.from_public_key(
            issuer=issuer,
            public_key=loaded,
            not_before=not_before,
            not_after=not_after,
        )

    @classmethod
    def from_public_file(
        cls,
        *,
        issuer: str,
        path: str | Path,
        not_before: datetime | None = None,
        not_after: datetime | None = None,
    ) -> TrustedGovernanceKey:
        try:
            pem = Path(path).read_bytes()
        except OSError as exc:
            raise GovernanceKeyLoadError(f"unable to read governance public key: {path}") from exc
        return cls.from_public_pem(
            issuer=issuer,
            pem=pem,
            not_before=not_before,
            not_after=not_after,
        )


class GovernanceSigner:
    """Ed25519 signer whose private material is never exposed in repr output."""

    __slots__ = ("_private_key", "issuer")

    def __init__(self, *, issuer: str, private_key: Ed25519PrivateKey) -> None:
        if not issuer or issuer.strip() != issuer:
            raise GovernanceCryptoError("issuer must be a non-empty trimmed value")
        self.issuer = issuer
        self._private_key = private_key

    def __repr__(self) -> str:
        return f"GovernanceSigner(issuer={self.issuer!r}, key_id={self.key_id!r})"

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    @property
    def issuer_pubkey(self) -> str:
        return public_key_b64(self.public_key)

    @property
    def key_id(self) -> str:
        return public_key_id(self.public_key)

    def sign(self, payload: bytes) -> str:
        if not isinstance(payload, bytes):
            raise TypeError("governance signature payload must be bytes")
        return _b64url_encode(self._private_key.sign(payload))

    def trusted_record(
        self,
        *,
        not_before: datetime | None = None,
        not_after: datetime | None = None,
    ) -> TrustedGovernanceKey:
        return TrustedGovernanceKey.from_public_key(
            issuer=self.issuer,
            public_key=self.public_key,
            not_before=not_before,
            not_after=not_after,
        )

    def private_key_pem(self, *, password: bytes | None = None) -> bytes:
        encryption: serialization.KeySerializationEncryption
        if password is None:
            encryption = serialization.NoEncryption()
        else:
            if not password:
                raise GovernanceKeyLoadError("private-key password must not be empty")
            encryption = serialization.BestAvailableEncryption(password)
        return self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=encryption,
        )

    def public_key_pem(self) -> bytes:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    @classmethod
    def generate(cls, *, issuer: str) -> GovernanceSigner:
        return cls(issuer=issuer, private_key=Ed25519PrivateKey.generate())

    @classmethod
    def from_private_pem(
        cls,
        *,
        issuer: str,
        pem: bytes,
        password: bytes | None = None,
    ) -> GovernanceSigner:
        _validate_pem_size(pem, field_name="governance private key PEM")
        try:
            loaded = serialization.load_pem_private_key(pem, password=password)
        except (TypeError, ValueError) as exc:
            raise GovernanceKeyLoadError("unable to load governance private key PEM") from exc
        if not isinstance(loaded, Ed25519PrivateKey):
            raise GovernanceKeyLoadError("governance private key must use Ed25519")
        return cls(issuer=issuer, private_key=loaded)

    @classmethod
    def from_private_file(
        cls,
        *,
        issuer: str,
        path: str | Path,
        password: bytes | None = None,
        require_private_permissions: bool = True,
    ) -> GovernanceSigner:
        resolved = Path(path)
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(resolved, flags)
        except OSError as exc:
            raise GovernanceKeyLoadError(
                f"unable to open governance private key safely: {resolved}"
            ) from exc
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise GovernanceKeyLoadError("governance private key must be a regular file")
            if require_private_permissions and os.name == "posix":
                insecure_bits = stat.S_IMODE(file_stat.st_mode) & 0o077
                if insecure_bits:
                    raise InsecurePrivateKeyPermissions(
                        "governance private key must not grant group/other permissions; "
                        f"found mode {stat.S_IMODE(file_stat.st_mode):04o}"
                    )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                pem = handle.read(MAX_KEY_PEM_BYTES + 1)
        except OSError as exc:
            raise GovernanceKeyLoadError(
                f"unable to read governance private key: {resolved}"
            ) from exc
        finally:
            os.close(descriptor)
        _validate_pem_size(pem, field_name="governance private key PEM")
        return cls.from_private_pem(issuer=issuer, pem=pem, password=password)

    @classmethod
    def from_private_env(
        cls,
        *,
        issuer: str,
        variable: str,
        password: bytes | None = None,
    ) -> GovernanceSigner:
        if not variable:
            raise GovernanceKeyLoadError("private-key environment variable name is required")
        value = os.environ.get(variable)
        if value is None:
            raise GovernanceKeyLoadError(
                f"governance private-key environment variable is unset: {variable}"
            )
        return cls.from_private_pem(
            issuer=issuer,
            pem=value.encode("utf-8"),
            password=password,
        )


class GovernanceKeyring:
    """Immutable trust store for active and retired governance public keys."""

    __slots__ = ("_keys",)

    def __init__(self, keys: tuple[TrustedGovernanceKey, ...] = ()) -> None:
        records: dict[str, TrustedGovernanceKey] = {}
        for key in keys:
            existing = records.get(key.key_id)
            if existing is not None and existing != key:
                raise GovernanceCryptoError(
                    f"conflicting governance key record: {key.key_id}"
                )
            records[key.key_id] = key
        self._keys: Mapping[str, TrustedGovernanceKey] = MappingProxyType(records)

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))

    def with_key(self, key: TrustedGovernanceKey) -> GovernanceKeyring:
        existing = self._keys.get(key.key_id)
        if existing is not None and existing != key:
            raise GovernanceCryptoError(
                f"conflicting governance key record: {key.key_id}"
            )
        return GovernanceKeyring(tuple(self._keys.values()) + (key,))

    def revoke(self, key_id: str) -> GovernanceKeyring:
        record = self._keys.get(key_id)
        if record is None:
            raise UntrustedGovernanceKey(f"governance key is not trusted: {key_id}")
        updated = tuple(
            replace(key, revoked=True) if key.key_id == key_id else key
            for key in self._keys.values()
        )
        return GovernanceKeyring(updated)

    def trusted_key(
        self,
        *,
        issuer: str,
        issuer_pubkey: str,
        now: datetime | None = None,
    ) -> TrustedGovernanceKey:
        key_id = public_key_id(issuer_pubkey)
        record = self._keys.get(key_id)
        if record is None:
            raise UntrustedGovernanceKey(f"governance key is not trusted: {key_id}")
        if record.issuer != issuer:
            raise UntrustedGovernanceKey(
                f"governance key issuer mismatch: {issuer} != {record.issuer}"
            )
        if record.issuer_pubkey != issuer_pubkey:
            raise UntrustedGovernanceKey("embedded public key does not match trusted key")
        record.assert_active(now=now)
        return record

    def verify(
        self,
        *,
        payload: bytes,
        issuer: str,
        issuer_pubkey: str,
        signature: str,
        now: datetime | None = None,
    ) -> str:
        if not isinstance(payload, bytes):
            raise TypeError("governance signature payload must be bytes")
        record = self.trusted_key(
            issuer=issuer,
            issuer_pubkey=issuer_pubkey,
            now=now,
        )
        try:
            signature_bytes = _b64url_decode(
                signature,
                expected_bytes=SIGNATURE_BYTES,
                field_name="signature",
            )
        except GovernanceCryptoError as exc:
            raise GovernanceSignatureError(str(exc)) from exc
        try:
            record.public_key.verify(signature_bytes, payload)
        except InvalidSignature as exc:
            raise GovernanceSignatureError("governance signature verification failed") from exc
        return record.key_id
