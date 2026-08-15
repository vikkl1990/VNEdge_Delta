import json
from datetime import UTC, datetime, timedelta

import pytest

from vnedge.exchange.delta_account import DeltaReadOnlyAccountProvider
from vnedge.exchange.delta_contracts import DeltaContractSpec
from vnedge.exchange.delta_private_stream import DeltaPrivateStream
from vnedge.execution.journal import DecisionJournal
from vnedge.governance.crypto import GovernanceKeyring, GovernanceSigner
from vnedge.governance.signed_envelope import SignedStageAuthorization
from vnedge.runtime.live_trader_main import _secret_value


class SafetyTruth:
    def __init__(self, *, position_size=0, balance=500.0):
        self.position_size = position_size
        self.balance = balance

    def get_position(self, product_id):
        return {"product_id": product_id, "size": self.position_size, "entry_price": 100.0}

    def get_wallet_balances(self):
        return [{"asset_symbol": "USD", "balance": self.balance}]


class OrderManagerStub:
    def apply_venue_order_update(self, **kwargs):
        return True

    def apply_venue_fill_update(self, **kwargs):
        return True

    def client_id_for_exchange_order(self, exchange_order_id):
        return None


async def test_delta_account_values_contract_positions_and_daily_loss(tmp_path):
    truth = SafetyTruth(position_size=3, balance=500.0)
    spec = DeltaContractSpec(
        symbol="ETHUSD",
        product_id=1,
        contract_value=1.0,
        contract_unit_currency="USD",
    )
    provider = DeltaReadOnlyAccountProvider(
        safety_client=truth,
        product_ids={"ETH/USD:USD": 1},
        contract_specs={"ETH/USD:USD": spec},
        base_currency="USD",
        equity_ledger_path=tmp_path / "equity.json",
        loss_streak_ledger_path=tmp_path / "streak.json",
    )
    first = await provider.account_state()
    assert first.open_positions == 1
    assert first.total_exposure_usd == pytest.approx(3.0)
    truth.balance = 490.0
    second = await provider.account_state()
    assert second.daily_pnl_usd == pytest.approx(-10.0)


async def test_delta_account_persists_completed_loss_streak(tmp_path):
    truth = SafetyTruth(position_size=0, balance=500.0)
    spec = DeltaContractSpec(
        symbol="ETHUSD",
        product_id=1,
        contract_value=1.0,
        contract_unit_currency="USD",
    )
    kwargs = {
        "safety_client": truth,
        "product_ids": {"ETH/USD:USD": 1},
        "contract_specs": {"ETH/USD:USD": spec},
        "base_currency": "USD",
        "equity_ledger_path": tmp_path / "equity.json",
        "loss_streak_ledger_path": tmp_path / "streak.json",
    }
    provider = DeltaReadOnlyAccountProvider(**kwargs)
    assert (await provider.account_state()).consecutive_losses == 0
    truth.position_size = 3
    assert (await provider.account_state()).consecutive_losses == 0
    truth.balance = 495.0
    truth.position_size = 0
    assert (await provider.account_state()).consecutive_losses == 1

    # A restart must not silently reset the risk counter.
    restarted = DeltaReadOnlyAccountProvider(**kwargs)
    assert (await restarted.account_state()).consecutive_losses == 1

    truth.position_size = 2
    await restarted.account_state()
    truth.balance = 505.0
    truth.position_size = 0
    assert (await restarted.account_state()).consecutive_losses == 0


def test_delta_private_auth_and_sequence_gap_fail_closed():
    stream = DeltaPrivateStream(
        api_key="key",
        api_secret="secret",
        symbols=("ETHUSD",),
        order_manager=OrderManagerStub(),
    )
    auth = stream._auth_message()
    assert auth["type"] == "key-auth"
    assert len(auth["payload"]["signature"]) == 64
    stream.apply_message({"type": "positions", "symbol": "ETHUSD", "sequence_id": 1})
    with pytest.raises(RuntimeError, match="sequence gap"):
        stream.apply_message({"type": "positions", "symbol": "ETHUSD", "sequence_id": 3})
    assert stream.health.connected is False


def test_hash_linked_live_journal_detects_tampering(tmp_path):
    path = tmp_path / "live.jsonl"
    journal = DecisionJournal(path, hash_chain=True)
    assert journal.append("one", {"value": 1})
    assert journal.append("two", {"value": 2})
    rows = journal.read_all()
    assert rows[0]["prev_hash"] == "0" * 64
    assert rows[1]["prev_hash"] == rows[0]["hash"]
    rows[0]["payload"]["value"] = 999
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    reopened = DecisionJournal(path, hash_chain=True)
    assert reopened.available is False


def test_secret_file_fallback(monkeypatch, tmp_path):
    secret = tmp_path / "api_secret"
    secret.write_text("mounted-secret\n")
    monkeypatch.delenv("VNEDGE_EXEC_API_SECRET", raising=False)
    monkeypatch.setenv("VNEDGE_EXEC_API_SECRET_FILE", str(secret))
    assert _secret_value("VNEDGE_EXEC_API_SECRET") == "mounted-secret"


def test_stage_authorization_signature_binds_transition():
    signer = GovernanceSigner.generate(issuer="ops")
    auth = SignedStageAuthorization.issue(
        signer=signer,
        strategy_id="s1",
        symbol="ETH/USD:USD",
        source_commit="abc",
        config_sha256="1" * 64,
        current_stage="shadow",
        target_stage="live_small",
        claims={"approved": True},
        created_at=datetime.now(UTC),
        ttl=timedelta(minutes=5),
    )
    keyring = GovernanceKeyring([signer.trusted_record()])
    assert auth.verify(
        keyring=keyring,
        strategy_id="s1",
        symbol="ETH/USD:USD",
        source_commit="abc",
        config_sha256="1" * 64,
        target_stage="live_small",
    ) == ()
