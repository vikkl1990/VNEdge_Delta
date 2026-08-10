"""Short-lived session JWTs — the anonymous-token account model.

Pins: a valid session token authenticates like the root token; it expires; a
tampered / wrong-alg / non-JWT token never authenticates (falls back to the
store); and issuing one grants no capability the root token didn't have.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.auth import DashboardUser, TokenStore
from vnedge.dashboard.session import SessionIssuer


# ------------------------------------------------------------------ unit
def test_issue_and_verify_roundtrip():
    iss = SessionIssuer(b"secret-key")
    tok = iss.issue("vik", "operator")
    res = iss.verify(tok.token)
    assert res and res.authorized and res.name == "vik" and res.role == "operator"


def test_expired_session_is_rejected():
    iss = SessionIssuer(b"secret-key", ttl_seconds=60)
    past = datetime.now(UTC) - timedelta(hours=1)
    tok = iss.issue("vik", "operator", now=past)
    res = iss.verify(tok.token)
    assert res is not None and not res.authorized and "expired" in (res.reason or "")


def test_tampered_and_foreign_tokens_do_not_verify():
    iss = SessionIssuer(b"secret-key")
    tok = iss.issue("vik", "operator").token
    assert iss.verify(tok[:-2] + "xx") is None  # broken signature
    assert SessionIssuer(b"other-key").verify(tok) is None  # wrong secret
    assert iss.verify("not-a-jwt") is None  # not a JWT → try the store instead
    assert iss.verify("a.b.c") is None  # 3 parts but garbage


def test_alg_confusion_none_is_rejected():
    import base64
    import json

    def b64(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()

    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': 'x', 'role': 'operator', 'exp': 9999999999})}."
    assert SessionIssuer(b"secret-key").verify(forged) is None


# ------------------------------------------------------------- integration
def _client():
    store = TokenStore([
        DashboardUser(name="viewer1", token="vt", role="viewer"),
        DashboardUser(name="op1", token="ot", role="operator"),
    ])
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    return TestClient(create_app(provider, token_store=store, session_issuer=SessionIssuer(b"test-secret")))


def test_session_endpoint_mints_a_working_jwt():
    client = _client()
    assert client.post("/auth/session").status_code == 401  # needs the root token
    r = client.post("/auth/session?token=ot")
    body = r.json()
    assert r.status_code == 200 and body["role"] == "operator"
    jwt = body["token"]
    # the JWT now authenticates a data route, and /whoami reflects the role
    who = client.get(f"/whoami?token={jwt}").json()
    assert who["role"] == "operator" and who["name"] == "op1"
    assert client.get(f"/state?token={jwt}").status_code == 200


def test_session_preserves_not_escalates_role():
    client = _client()
    jwt = client.post("/auth/session?token=vt").json()["token"]  # viewer root
    who = client.get(f"/whoami?token={jwt}").json()
    assert who["role"] == "viewer"  # session role == token role, never elevated
