# Delta Correctness Contract

Status: active local policy, 10 August 2026.

## Identity

VNEdge_Delta is a local Delta India research laboratory for BTCUSD and ETHUSD.
It has no validated after-cost edge and is neither paper-enabled nor
live-enabled.

## Runtime invariants

| Invariant | Required value | Enforcement |
|---|---:|---|
| `research_only` | `true` | Frozen Delta configuration and payloads |
| `can_trade` | `false` | Literal config type and runtime validation |
| `can_promote` | `false` | Config validator and research payloads |
| `validated_edge` | `false` | Literal config type and dashboard identity |
| `order_route` | `absent` | Literal config type; sidecars construct none |
| `broker` | `absent` | Literal config type; sidecars construct none |

The live-data Delta candle sidecar calls `assert_runtime_safe()` before it
constructs its research assembly. Momentum Burst, Imbalance Fade, and
Hierarchical Pullback therefore remain reproducible in historical replay but
cannot be activated in that continuously running process.

## Active direction

The only active direction is event-time data integrity and deterministic
replay:

1. Record exact public wire events with exchange time, local receive time, and
   local monotonic time.
2. Validate L2 snapshot, sequence, and checksum continuity.
3. Expose measured feed-delay percentiles and integrity-fault counts.
4. Reproduce identical feature/candidate streams through causal replay.
5. Preregister an event hypothesis only after the replay contract passes.

The live recorder's observer is feature-only. Event scanner algorithms may be
used by isolated replay research, but they are not instantiated by the live
recorder runtime.

## Economic and validation rules

- Frequency is an output, never a target.
- Every result is after realistic Delta fees, GST, and slippage unless labelled
  gross.
- The default aggressive-execution model is 5 bps per leg, plus 18% GST on
  the fee and 1.5 bps slippage per leg: 14.8 bps round trip. Fee-only scenario
  checks are 4.72 bps maker/maker, 8.26 bps maker/taker, and 11.8 bps
  taker/taker. The Scalper Offer and DETO discount remain disabled unless an
  experiment explicitly opts in and proves eligibility.
- Funding is not inferred from hold duration. A multi-hour replay is all-in
  only when every settlement crossed by the position is causally aligned and
  passed into the fee breakdown. Otherwise the result must state
  `funding_used=false` and is optimistic by the unknown funding amount.
- BTCUSD uses a 0.001 BTC minimum contract size; ETHUSD uses 0.01 ETH. Product
  metadata remains the execution source of truth instead of a shared hardcoded
  lot size.
- A scanner must first be independently net-positive; regime and meta-model
  filters cannot rescue a losing primary scanner.
- The chronological selection window must pass before one sealed untouched
  evaluation is opened.
- A failed frozen version remains retired. A materially different idea gets a
  new version and a new preregistration.

## Truth surfaces

The root README and default dashboard must always say:

- Delta India research laboratory;
- BTCUSD and ETHUSD primary scope;
- no validated after-cost edge;
- all primary scanners disabled;
- paper/live trading locked;
- no broker or order route.

The generic multi-venue dashboard remains secondary under `/research-lab` and
does not define the product identity.

The supported launcher is `scripts/start_delta_research_dashboard.sh`. The
older `start_local_paper_dashboard.sh` belongs to the historical generic
runtime and is not the VNEdge_Delta entry point.
