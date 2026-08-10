from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from vnedge.governance.crypto import (
    GovernanceCryptoError,
    GovernanceKeyLoadError,
    GovernanceKeyring,
    GovernanceSignatureError,
    GovernanceSigner,
    InactiveGovernanceKey,
    InsecurePrivateKeyPermissions,
    TrustedGovernanceKey,
    UntrustedGovernanceKey,
    generate_nonce,
    public_key_id,
    validate_nonce,
)


def test_trusted_keyring_verifies_detached_ed25519_signature():
    signer = GovernanceSigner.generate(issuer="promotion-policy-evaluator")
    keyring = GovernanceKeyring((signer.trusted_record(),))
    payload = b'{"proof_hash":"abc123"}'

    verified_key_id = keyring.verify(
        payload=payload,
        issuer=signer.issuer,
        issuer_pubkey=signer.issuer_pubkey,
        signature=signer.sign(payload),
    )

    assert verified_key_id == signer.key_id
    assert verified_key_id == public_key_id(signer.issuer_pubkey)
    assert "PRIVATE" not in repr(signer)


def test_signature_verification_rejects_payload_tampering():
    signer = GovernanceSigner.generate(issuer="governance")
    keyring = GovernanceKeyring((signer.trusted_record(),))
    signature = signer.sign(b"original")

    with pytest.raises(GovernanceSignatureError, match="verification failed"):
        keyring.verify(
            payload=b"tampered",
            issuer=signer.issuer,
            issuer_pubkey=signer.issuer_pubkey,
            signature=signature,
        )


def test_embedded_public_key_is_not_trusted_automatically():
    trusted = GovernanceSigner.generate(issuer="governance")
    attacker = GovernanceSigner.generate(issuer="governance")
    keyring = GovernanceKeyring((trusted.trusted_record(),))
    payload = b"forged proof"

    with pytest.raises(UntrustedGovernanceKey, match="not trusted"):
        keyring.verify(
            payload=payload,
            issuer=attacker.issuer,
            issuer_pubkey=attacker.issuer_pubkey,
            signature=attacker.sign(payload),
        )


def test_keyring_binds_trusted_key_to_issuer_identity():
    signer = GovernanceSigner.generate(issuer="human-governance")
    keyring = GovernanceKeyring((signer.trusted_record(),))
    payload = b"approval"

    with pytest.raises(UntrustedGovernanceKey, match="issuer mismatch"):
        keyring.verify(
            payload=payload,
            issuer="research-agent",
            issuer_pubkey=signer.issuer_pubkey,
            signature=signer.sign(payload),
        )


def test_key_validity_window_and_revocation_fail_closed():
    now = datetime(2026, 8, 8, 12, tzinfo=UTC)
    signer = GovernanceSigner.generate(issuer="governance")
    future_record = signer.trusted_record(
        not_before=now + timedelta(minutes=1),
        not_after=now + timedelta(days=1),
    )

    with pytest.raises(InactiveGovernanceKey, match="not active yet"):
        GovernanceKeyring((future_record,)).trusted_key(
            issuer=signer.issuer,
            issuer_pubkey=signer.issuer_pubkey,
            now=now,
        )

    active_record = signer.trusted_record(
        not_before=now - timedelta(days=1),
        not_after=now + timedelta(days=1),
    )
    active_keyring = GovernanceKeyring((active_record,))
    revoked_keyring = active_keyring.revoke(signer.key_id)

    assert len(active_keyring) == 1
    with pytest.raises(InactiveGovernanceKey, match="revoked"):
        revoked_keyring.trusted_key(
            issuer=signer.issuer,
            issuer_pubkey=signer.issuer_pubkey,
            now=now,
        )


def test_key_rotation_returns_new_immutable_keyring():
    first = GovernanceSigner.generate(issuer="governance-v1")
    second = GovernanceSigner.generate(issuer="governance-v2")
    original = GovernanceKeyring((first.trusted_record(),))

    rotated = original.with_key(second.trusted_record())

    assert original.key_ids == (first.key_id,)
    assert set(rotated.key_ids) == {first.key_id, second.key_id}


def test_private_pem_round_trip_supports_encryption():
    signer = GovernanceSigner.generate(issuer="governance")
    encrypted = signer.private_key_pem(password=b"strong-test-password")

    restored = GovernanceSigner.from_private_pem(
        issuer="governance",
        pem=encrypted,
        password=b"strong-test-password",
    )

    assert restored.key_id == signer.key_id
    with pytest.raises(GovernanceKeyLoadError, match="unable to load"):
        GovernanceSigner.from_private_pem(
            issuer="governance",
            pem=encrypted,
            password=b"wrong-password",
        )


def test_private_file_loader_enforces_posix_permissions(tmp_path):
    signer = GovernanceSigner.generate(issuer="governance")
    key_path = tmp_path / "governance-private.pem"
    key_path.write_bytes(signer.private_key_pem())
    key_path.chmod(0o600)

    restored = GovernanceSigner.from_private_file(
        issuer="governance",
        path=key_path,
    )
    assert restored.key_id == signer.key_id

    key_path.chmod(0o644)
    with pytest.raises(InsecurePrivateKeyPermissions, match="group/other"):
        GovernanceSigner.from_private_file(issuer="governance", path=key_path)


def test_private_file_loader_rejects_symlinks(tmp_path):
    signer = GovernanceSigner.generate(issuer="governance")
    target = tmp_path / "target.pem"
    link = tmp_path / "link.pem"
    target.write_bytes(signer.private_key_pem())
    target.chmod(0o600)
    link.symlink_to(target)

    with pytest.raises(GovernanceKeyLoadError, match="open.*safely"):
        GovernanceSigner.from_private_file(issuer="governance", path=link)


def test_public_pem_and_environment_private_key_loading(monkeypatch):
    signer = GovernanceSigner.generate(issuer="governance")
    record = TrustedGovernanceKey.from_public_pem(
        issuer="governance",
        pem=signer.public_key_pem(),
    )
    monkeypatch.setenv("VNEDGE_TEST_GOVERNANCE_KEY", signer.private_key_pem().decode())

    restored = GovernanceSigner.from_private_env(
        issuer="governance",
        variable="VNEDGE_TEST_GOVERNANCE_KEY",
    )

    assert record.key_id == signer.key_id
    assert restored.key_id == signer.key_id


def test_non_ed25519_private_key_is_rejected():
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    with pytest.raises(GovernanceKeyLoadError, match="must use Ed25519"):
        GovernanceSigner.from_private_pem(issuer="governance", pem=rsa_pem)


def test_nonce_is_256_bit_unique_and_strictly_validated():
    nonces = {generate_nonce() for _ in range(100)}

    assert len(nonces) == 100
    for nonce in nonces:
        validate_nonce(nonce)
    with pytest.raises(GovernanceCryptoError, match="nonce"):
        validate_nonce("too-short")
    canonical = generate_nonce()
    with pytest.raises(GovernanceCryptoError, match="canonical"):
        validate_nonce(canonical + "=")
