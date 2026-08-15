# Delta India production ladder

This release implements the production **paths**, but it does not assert that
the current scanner has an edge. The runtime is intentionally blocked while
`mtf_amf_directional_rejection_v3` has only 15 of the preregistered 60
selection trades and its untouched period remains sealed.

## Implemented phases

| Phase | Implementation | Current authority |
|---|---|---|
| Research | Causal selection evidence and authoritative readiness publisher | Available |
| Observation | Versioned scanner contract; no fills or orders | Blocked by failed sample gate |
| Paper | Signed `PaperEligibilityProof`, trusted Ed25519 keyring, single-use nonce, venue-bound manifest and canonical GST-inclusive Delta cost contract | Route complete but dormant; the current Delta scanner remains ineligible while selection fails |
| Shadow | Required as a signed lower-rung proof claim | Code complete; evidence absent |
| Live small | Delta-native REST execution, authenticated private WS, REST account truth, deadman, atomic bracket, risk gateway, reconciliation | Code complete; authorization absent |
| Live full | Requires a signed live-small proof in addition to all lower rungs | Code complete; authorization absent |

Dashboard status comes from `configs/production_live.yaml` plus the immutable
selection artifact, published by:

```bash
python -m vnedge.runtime.scanner_authority \
  --manifest configs/production_live.yaml \
  --out research/live_research/production_readiness_latest.json
```

The publisher never infers permission from a filename. Failed gate checks are
listed as blockers and `can_trade` / `can_promote` remain false.

## Release checklist

1. Checkout a reviewed commit. Record `git rev-parse HEAD`; never use `dev`.
2. Run `python -m pytest -q` and `ruff check src tests` in CI.
   The release job also runs `vnedge.runtime.production_release_check`, which
   rejects dirty/untraceable source, an unpinned production dependency set, or
   a production manifest whose safety defaults drift open.
3. Stop and reconcile `delta-live-small`, then back up state with
   `scripts/backup_production_state.sh <offline-path>` and verify the generated
   SHA-256 file. The script refuses while the live service is running and
   preserves SQLite WAL/SHM files with the database.
4. Create two Delta India keys: a read-only audit key for manual checks and a
   trade-only, withdrawal-disabled, IP-whitelisted execution key. Do not put
   either key in `.env`.
5. Store the execution key and secret in root-readable files outside Git and
   set `VNEDGE_DELTA_API_KEY_FILE` / `VNEDGE_DELTA_API_SECRET_FILE` to them.
6. Put the trusted public keyring and one signed, unexpired stage authorization
   in `deploy/governance/`. Private governance keys never go on the bot host.
7. Bind the authorization to the exact commit SHA, runtime-config SHA-256,
   strategy, symbol, previous stage and requested stage. Its claims must carry
   immutable hashes for untouched, paper and shadow evidence. `live_full` also
   needs live-small evidence.
8. On the host, create writable state owned by uid/gid 10001:

   ```bash
   sudo install -d -o 10001 -g 10001 data logs logs/live
   ```

9. Keep `data/LIVE_KILL` present until the change window begins. Removing it
   is an explicit operator action; the program never auto-resets it.
10. Validate the compose expansion without starting anything:

   ```bash
   VNEDGE_BUILD_SHA=$(git rev-parse HEAD) \
   VNEDGE_LIVE_STRATEGY=<authorized-strategy> \
   VNEDGE_DELTA_API_KEY_FILE=/secure/path/key \
   VNEDGE_DELTA_API_SECRET_FILE=/secure/path/secret \
   VNEDGE_DELTA_ACCOUNT_CURRENCY=<verified-settlement-wallet> \
   docker compose --profile live config --quiet
   ```

11. Start only the live-small service, initially with live gates closed. It
    must exit before creating a client. Then open the three gates during the
    reviewed change window:

   ```bash
   COMPOSE_PROFILES=live docker compose run --rm delta-live-small
   ```

12. Before the first entry the process must independently confirm: private WS
    authentication/freshness, deadman armed, REST order/position truth clean,
    valid hash-linked journal, no kill switch, exact signed transition, and
    all risk checks. Any failure is terminal for that start.

## Recovery and rollback

- A private-stream gap, stale feed, deadman failure, journal failure, ambiguous
  order or REST mismatch blocks new risk. Reduce-only exits remain available.
- To stop new entries, create `data/LIVE_KILL`. Do not delete it until the
  account and journal have been reconciled by two independent checks.
- The deadman cancels risk-increasing resting orders if the process stops
  acknowledging it. Exchange-resident bracket protection remains attached.
- Roll back by stopping the live service, confirming Delta orders/positions,
  checking out the last reviewed commit, restoring state only from a verified
  backup, and issuing a **new** signed authorization. Nonces cannot be reused.
- Never edit a live hash-linked journal. Archive it and investigate; a broken
  chain is a hard refusal, not a repair-in-place event.

## Governed paper route

Paper is a separate opt-in profile and does not carry Delta API credentials.
Place an operator-issued `paper_manifest.yaml`, its signed eligibility proof,
and the trusted public `keyring.json` under `deploy/governance/`. The proof must
bind the exact strategy parameters, source commit, `delta_india` venue, and the
canonical GST-inclusive cost contract. Then run:

```bash
VNEDGE_BUILD_SHA=$(git rev-parse HEAD) \
COMPOSE_PROFILES=paper docker compose run --rm delta-governed-paper
```

The eligibility nonce is consumed once in SQLite. Expired, replayed, tampered,
wrong-venue, or cost-drifted manifests fail before the public market feed is
opened. The route still performs simulated fills only and cannot reach the
live-order adapter.

## What remains blocked today

The execution machinery is not the blocker. Scanner evidence is. No paper or
live authorization should be issued until a standalone scanner passes its
selection gate, one sealed untouched evaluation, paper evidence, and shadow
evidence under the frozen fee/slippage model.
