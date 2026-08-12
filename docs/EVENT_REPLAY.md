# Deterministic Delta event replay

VNEDGE replays finalized Delta public-event recorder shards through the same
`DeltaVerifiedEventBridge` and `EventDrivenTriggerLayer` used by the live
research path. Replay has no broker, risk gateway, credential loader, or order
router. Every replay artifact is explicitly `research_only` and `can_trade=false`.

## Flow

```mermaid
flowchart LR
    A["Finalized recorder shards"] --> B["Manifest SHA-256 verification"]
    B --> C["Sequence + book checksum validation"]
    C --> D["Causal local-receive-order merge"]
    D --> E["Shared verified event bridge"]
    E --> F["Shared incremental feature state"]
    F --> G["Shared scanners and gates"]
    G --> H["Deterministic replay journal"]
    G --> I["Next-trade MFE / MAE tracker"]
    H --> J["Replay result + state hash"]
    I --> J
```

Validation completes before any event reaches a scanner. A bad shard hash,
sequence gap, checksum mismatch, unsupported code version, or forbidden holdout
overlap stops the run before research decisions are produced.

## CLI

Feature-only replay:

```bash
python -m vnedge.replay \
  --event-root data/delta_events \
  --symbols BTCUSD \
  --from 2026-08-01T00:00:00+00:00 \
  --to 2026-08-02T00:00:00+00:00 \
  --scanner none
```

Flow-imbalance replay uses the currently locked scanner prior and gates:

```bash
python -m vnedge.replay \
  --event-root data/delta_events \
  --symbols BTCUSD,ETHUSD \
  --from 2026-08-01T00:00:00+00:00 \
  --to 2026-08-02T00:00:00+00:00 \
  --scanner flow
```

Absorption replay requires explicit exchange tick sizes. VNEDGE refuses to
guess them:

```bash
python -m vnedge.replay \
  --event-root data/delta_events \
  --symbols BTCUSD,ETHUSD \
  --from 2026-08-01T00:00:00+00:00 \
  --to 2026-08-02T00:00:00+00:00 \
  --scanner absorption \
  --tick-size BTCUSD=0.5 \
  --tick-size ETHUSD=0.05
```

The default CLI uses the Git commit for a clean tree. During local development
it derives a SHA-256 identifier over the replay-relevant source and
configuration tree, so an uncommitted run still has a reproducible content
identity.

Feature-state determinism proof:

```bash
python -m vnedge.replay \
  --event-root data/delta_events \
  --symbols BTCUSD,ETHUSD \
  --from 2026-08-10T03:23:49+00:00 \
  --to 2026-08-10T03:26:45+00:00 \
  --scanner none \
  --no-journal \
  --verify-determinism
```

This runs two independent replays and atomically writes
`research/event_replay/replay_determinism_latest.json`. Empty windows and
feature-enabled runs with no feature snapshots fail closed.

## Holdout manifest

`--sealed-holdout` requires a manifest and fails if the requested window
overlaps a declared development interval. The requested window must also be
fully contained by a declared sealed interval. Conversely, a window declared
sealed cannot be replayed without the explicit `--sealed-holdout` flag.

```json
{
  "schema_version": "vnedge.event_replay_holdout.v1",
  "development_windows": [
    {
      "name": "selection-2026-08",
      "start_ts_us": 1785522600000000,
      "end_ts_us": 1786127400000000,
      "symbols": ["BTCUSD", "ETHUSD"]
    }
  ],
  "sealed_windows": [
    {
      "name": "untouched-2026-09",
      "start_ts_us": 1788201000000000,
      "end_ts_us": 1790793000000000,
      "symbols": ["BTCUSD", "ETHUSD"]
    }
  ]
}
```

## Interpretation

The deterministic hash covers ordered event identities, incremental feature
snapshots, accepted candidates, and counterfactual absorption records.
Performance timings are deliberately excluded because wall-runtime varies
between machines.
Candidate outcomes use the next recorded trade as the causal entry, then track
tick-path MFE, MAE, stop, first target, time-stop, modeled costs, and net bps.
An incomplete replay tail is recorded but excluded from economic expectancy.

## Continuity qualification

The failed-auction data gate is evaluated from the newest recoverable recorder
epoch, not from a stitched whole-directory event count. A qualification epoch:

- starts only after verified BTCUSD and ETHUSD order-book snapshots;
- may survive a WebSocket reconnect only after both fresh snapshots validate;
- splits on sequence, checksum, protocol, parse, or subscription integrity faults;
- never crosses recorder session boundaries;
- excludes active `.partial` writer files and separately blocks orphan partials;
- validates and reports only finalized manifest-backed shards.

The local dashboard refreshes the qualification artifact every five minutes.
It displays qualified events and days, the last hard reset, active versus orphan
partials, shard verification, and the remaining target. These are research
metrics and always publish `can_trade=false` and `can_promote=false`.

Replay evidence is not proof of live fillability. Queue position, packet loss
outside the recorder, and venue execution latency still require shadow and
paper validation after a scanner passes sealed research gates.
