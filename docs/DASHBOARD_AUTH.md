# Dashboard auth — per-user tokens, roles, expiry

The read-only dashboard (docs/DESIGN.md §6) authenticates every data route
(`/state`, `/history`, `/research`, `/alpha-council`, `/alpha-workbench`) and
the snapshot WebSocket (`/ws`). The static shell page (`/`) stays public — it
contains no data.

Auth is implemented in `src/vnedge/dashboard/auth.py` and wired in
`src/vnedge/dashboard/app.py`.

## Token sources

### `DASHBOARD_USERS` — per-user tokens

```
DASHBOARD_USERS="name:token:role[:expiry_iso];name:token:role[:expiry_iso];..."
```

One entry per user, joined by `;`. Fields per entry, separated by `:`:

| field       | required | values                                                        |
|-------------|----------|---------------------------------------------------------------|
| `name`      | yes      | identity used in logs and the `X-Dashboard-User` header       |
| `token`     | yes      | bearer secret — plaintext, **or** a salted hash (see *Hashed tokens* below) so the env holds no usable secret |
| `role`      | yes      | `viewer`, `operator`, or `auditor` (case-insensitive)         |
| `expiry_iso`| no       | ISO-8601 datetime, e.g. `2026-08-01T00:00:00+00:00`; omit for no expiry. Naive datetimes are treated as UTC. The expiry field may contain `:` — everything after the third colon is parsed as the expiry. |

Example:

```
DASHBOARD_USERS="vik:3KJ...a9:operator;auditor:9fX...q2:viewer:2026-08-01T00:00:00+00:00"
```

Parsing is defensive: a malformed entry (missing fields, empty name/token,
unknown role, unparseable expiry, duplicate name) is **skipped with a loud
WARNING** naming the entry position and problem — the token text is never
logged — and the remaining valid entries still load.

### `DASHBOARD_TOKEN` — back-compat shared token

The original single shared token **keeps working unchanged**: it is loaded as
the user `operator` with role `operator` and **no expiry**. Existing deploys
(docker-compose requires `DASHBOARD_TOKEN` in `.env`) need zero changes. Both
variables may be set at once; all tokens are valid simultaneously — that is
what makes zero-downtime rotation possible.

If neither variable yields at least one user, the dashboard refuses to start
("no token, no dashboard").

### Hashed tokens — the env holds no usable secret

The `token` field may be a **salted hash** instead of a plaintext secret, so a
leaked `.env` / compose config does not leak a working token. Generate one from
a raw token read on stdin (never argv, so it stays out of shell history / `ps`):

```
python -m vnedge.dashboard.auth hash
# paste/pipe the raw token when prompted; it prints e.g.
# vnedge-sha256$<salt_hex>$<digest_hex>
```

Put the printed `vnedge-sha256$…` value in the `token` field (in
`DASHBOARD_USERS` or as `DASHBOARD_TOKEN`); keep the **raw** token in your
password manager. The dashboard hashes each presented token with the stored
salt and compares constant-time. Plaintext tokens still work unchanged — a
stored value is only treated as a hash when it starts with `vnedge-sha256$` —
so this is opt-in and backward-compatible. Tokens are high-entropy secrets, so
a salted SHA-256 is sufficient (a slow password KDF would add latency to every
request and buy nothing against a 32-byte random token).

## Roles

`viewer`, `operator`, and `auditor`, mapped to permissions in
`auth.PERMISSIONS`:

| role       | permissions                                                       |
|------------|-------------------------------------------------------------------|
| `viewer`   | `view`                                                            |
| `auditor`  | `view`, `view_audit` (read the operator-action trail; **no** control) |
| `operator` | `view`, `view_audit`, `promote`, `flip_live_gate`, `kill_switch`  |

**Every current route is read-only** — the dashboard exposes zero control
routes, structurally, so `view` is all any role needs today. The map exists so
a future privileged surface (e.g. the v2 kill-switch button in DESIGN.md §6)
enforces server-side via `_require_permission(<perm>)` — a viewer gets `403` —
without another auth migration. `has_permission(role, perm)` is the primitive;
`GET /whoami` returns the caller's `name`, `role`, and `permissions` so a
frontend can hide controls it can't use (defense in depth; the server still
enforces). Grant `viewer` by default; `auditor` to review the audit trail;
`operator` only to those allowed to operate controls later.

## Health & readiness probes

Two **unauthenticated** probes (they reveal only process state, never data):

- `GET /health` — liveness: `200 {"status":"ok"}` as soon as the process
  serves. Used by the compose healthcheck + TLS proxy.
- `GET /ready` — readiness: `200 {"status":"ready"}` once a snapshot has been
  published, `503 {"status":"starting"}` while warming. Lets an orchestrator
  wait for data-readiness without treating a warming process as dead.

## Behavior

- Tokens are accepted via `Authorization: Bearer <token>` header or the
  `?token=` query parameter (the WebSocket uses the query parameter).
- Every stored token is compared with a constant-time comparison, and every
  token is checked on every attempt (no early exit), so timing does not
  reveal which entry matched.
- **Expired tokens are rejected with 401** and an explicit reason
  (`token expired at <iso>`), distinct from `missing or invalid token`.
  A WebSocket whose token expires mid-session is closed (code 4401).
- Authenticated HTTP responses carry `X-Dashboard-User: <name>` and
  `X-Dashboard-Role: <role>`.
- WebSocket snapshots include `dashboard_connections`: the **count** of live
  dashboard sockets. Names and tokens are never serialized into snapshots.
- Auth events are logged with name and role only — token values never appear
  in logs, responses, or snapshots.

## Rotation procedure

Zero-downtime, because old and new tokens can coexist:

1. Generate a new token:
   `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.
2. Add it as a **new entry** for the same person in `DASHBOARD_USERS`
   (entries need unique names — use e.g. `vik-2026q3`), optionally giving the
   **old** entry a near-future expiry instead of deleting it immediately.
3. Restart the service (env is read at startup):
   `docker compose up -d multi-lane-shadow` on the VPS, or restart the local
   session.
4. Update the person's bookmark/tunnel URL to the new token; confirm access
   (the `X-Dashboard-User` header shows which entry matched).
5. Remove the old entry (or let its expiry lapse) and restart again.

To revoke a user immediately: delete their entry (or set an expiry in the
past) and restart.

Rotating the legacy `DASHBOARD_TOKEN` is the same dance: set the new value in
`DASHBOARD_USERS` first, restart, verify, then change/remove
`DASHBOARD_TOKEN`.

## Operational notes

- Env changes require a restart — consistent with the frozen-risk-config
  rule; there is no runtime mutation surface for auth.
- Tokens are secrets: keep them in `.env` (gitignored) like every other
  secret in this repo; never commit them.
- The dashboard remains read-only regardless of role; this change adds
  identity and lifecycle to tokens, not capabilities.
