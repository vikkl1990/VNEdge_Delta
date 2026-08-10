# Crypto Bot Capability Radar

`vnedge.research.bot_capability_radar` turns the
[awesome-crypto-trading-bots](https://github.com/botcrypto-io/awesome-crypto-trading-bots)
landscape into a durable VNEDGE research artifact.

The source is a taxonomy, not proof. The awesome list explicitly says it does
not test the projects it links. VNEDGE therefore uses it only to extract
architecture patterns: market-making engines, strategy research frameworks,
isolated strategy runtimes, operator workspaces, exchange adapters, and charting
stacks.

## Why This Exists

We keep reviewing public bots and indicator ecosystems. The radar prevents that
work from evaporating into chat history. It answers:

- Which peer-bot capability patterns matter for VNEDGE?
- Which ones are already covered?
- Which are partial or missing?
- Which gaps actually matter for crypto scalping?

It never creates trade signals.

## Current Strategic Read

The radar intentionally ranks `maker_quote_lifecycle_engine` as the highest
scalper gap.

Reason: most successful scalper-like bots are not just indicator bots. They
have explicit quote lifecycle mechanics: post-only intent, cancel/replace,
queue/fill telemetry, inventory skew, adverse-selection measurement, and
venue-specific fee/precision handling. VNEDGE has research replay and an order
manager, but does not yet have a dedicated maker quote lifecycle that can be
replayed, shadowed, then papered.

High-priority build gaps:

| Capability | Status | Why it matters |
| --- | --- | --- |
| Maker quote lifecycle engine | Partial | Fee wall cannot be beaten by signal logic alone |
| AI strategy sandbox isolation | Gap | Agent-generated strategies need hard contracts |
| Terminal-grade operator UI | Partial | Operator needs dense tape/replay/rejection views |
| Multi-exchange adapter depth | Partial | Venue fees/fills decide whether an edge is tradable |
| Market data redundancy | Partial | Bad ticks create fake microstructure edge |

Watchlist:

- Grid/DCA retail bot modes are intentionally low priority. They can hide
  martingale exposure and do not solve daily crypto scalping.

## Artifact Contract

The payload includes:

- `radar_id=crypto_bot_competitive_radar_v1`
- `source.not_profit_evidence=true`
- `policy.can_trade=false`
- `policy.can_promote=false`
- `policy.live_orders_enabled=false`
- Ranked `capabilities`
- Ranked `top_builds`

## Run

```bash
python -m vnedge.research.bot_capability_radar --json
```

Optional status overrides are useful after a capability lands:

```bash
python -m vnedge.research.bot_capability_radar \
  --status-overrides maker_quote_lifecycle_engine=covered \
  --json
```

Default output:

```text
research/live_research/bot_capability_radar_latest.json
research/live_research/bot_capability_radar_feed.jsonl
```

## Promotion Discipline

This radar is above the signal funnel. It may identify the next engineering
build, but it cannot promote a strategy, paper trial, shadow lane, or live lane.
Those still require the normal replay, untouched judgment, human approval, and
risk-gateway path.
