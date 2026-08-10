"""Token hashing, RBAC permission map, and the /ready + /whoami surfaces.

The security property under test: a leaked *config* (hashed token in the env)
must not yield a working bearer token, while existing plaintext deploys keep
working unchanged; and roles must map to permissions server-side so a future
control route cannot be reached by a viewer.
"""

from fastapi.testclient import TestClient

from vnedge.dashboard.app import SnapshotProvider, create_app
from vnedge.dashboard.auth import (
    PERM_KILL_SWITCH,
    PERM_VIEW,
    PERM_VIEW_AUDIT,
    DashboardUser,
    TokenStore,
    has_permission,
    hash_token,
    is_hashed,
    parse_users_env,
    permissions_for,
    verify_token,
)


# --------------------------------------------------------------- token hashing
def test_hash_roundtrip_and_rejects_wrong_token():
    h = hash_token("s3cret-token")
    assert is_hashed(h) and h.startswith("vnedge-sha256$")
    assert verify_token("s3cret-token", h)
    assert not verify_token("wrong", h)


def test_hash_is_salted_so_two_hashes_of_same_token_differ():
    assert hash_token("same") != hash_token("same")  # random salt each time


def test_malformed_hash_is_refused_not_crashed():
    assert verify_token("anything", "vnedge-sha256$not-a-valid-hash") is False


def test_plaintext_still_verifies_backcompat():
    assert verify_token("plain", "plain")
    assert not verify_token("plain", "other")


def test_store_authenticates_a_hashed_token():
    store = TokenStore([DashboardUser(name="op", token=hash_token("raw-secret"), role="operator")])
    ok = store.authenticate("raw-secret")
    assert ok.authorized and ok.role == "operator"
    assert not store.authenticate("raw-secret-wrong").authorized


def test_hashed_token_parses_through_dashboard_users_env():
    h = hash_token("envtoken")
    users = parse_users_env(f"alice:{h}:auditor")
    assert len(users) == 1 and users[0].role == "auditor"
    assert TokenStore(users).authenticate("envtoken").authorized


# ---------------------------------------------------------------------- RBAC
def test_permission_map_per_role():
    assert has_permission("viewer", PERM_VIEW)
    assert not has_permission("viewer", PERM_VIEW_AUDIT)
    assert has_permission("auditor", PERM_VIEW_AUDIT)
    assert not has_permission("auditor", PERM_KILL_SWITCH)  # auditor cannot control
    assert has_permission("operator", PERM_KILL_SWITCH)
    assert not has_permission(None, PERM_VIEW)  # unknown role → nothing


def test_permissions_for_is_sorted_and_role_scoped():
    assert permissions_for("viewer") == [PERM_VIEW]
    assert PERM_VIEW_AUDIT in permissions_for("auditor")
    assert set(permissions_for("operator")) > set(permissions_for("auditor"))


# ---------------------------------------------------------- /ready and /whoami
def test_ready_probe_flips_with_snapshot():
    provider = SnapshotProvider()
    client = TestClient(create_app(provider, token="t"))
    assert client.get("/ready").status_code == 503  # unauthenticated, warming
    provider.publish({"mode": "shadow"})
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_ready_needs_no_token():
    client = TestClient(create_app(SnapshotProvider(), token="t"))
    # 503 (not 401) — readiness is unauthenticated, like /health
    assert client.get("/ready").status_code == 503


def test_whoami_reports_role_and_permissions():
    store = TokenStore([
        DashboardUser(name="viewer1", token="vt", role="viewer"),
        DashboardUser(name="op1", token="ot", role="operator"),
    ])
    provider = SnapshotProvider()
    provider.publish({"mode": "shadow"})
    client = TestClient(create_app(provider, token_store=store))

    assert client.get("/whoami").status_code == 401  # token required

    v = client.get("/whoami?token=vt")
    assert v.json()["role"] == "viewer"
    assert v.json()["permissions"] == [PERM_VIEW]
    assert v.headers["X-Dashboard-Role"] == "viewer"

    o = client.get("/whoami?token=ot").json()
    assert PERM_KILL_SWITCH in o["permissions"] and o["role"] == "operator"
