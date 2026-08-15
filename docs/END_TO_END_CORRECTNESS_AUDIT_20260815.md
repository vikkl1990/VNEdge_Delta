# VNEDGE Delta End-to-End Correctness Release — 2026-08-15

## Scope

This release corrects evidence, routing, runtime-truth, and dashboard ambiguity.
It does not claim alpha and does not add an order route.

## Corrected contracts

- The rejected Delta scalper population is repriced per trade under the single
  canonical `taker_full_14_8` contract. Historical maker flags are diagnostics,
  not evidence authority.
- `configs/strategy_registry.yaml` binds the displayed scalper metrics to the
  SHA-256 verified active-cost artifact.
- Paper activation now requires both explicit canonical registry authority and
  a valid signed `PaperEligibilityProof`. Legacy `approved_by: human` strings
  are displayed as labels only and cannot open a route.
- Dashboard sessions renew automatically after expiry; authenticated SSE and
  snapshot reads retry only after a fresh session is established.
- Recorder latency separates decision-eligible events from delayed historical
  backlog. Backlog remains visible but cannot create a false latency incident.
- Recorder, event trigger, Delta sidecar, activation publisher, and dashboard
  expose process code versions. Missing or differing revisions are shown as
  runtime drift.
- Trade-to-book truth uses the latest causal pre-trade snapshot and bounded
  tick-size matching, removing unsafe floating-point equality without allowing
  unbounded joins.
- Response-atlas and direction artifacts are withheld when their independent
  episode population differs from the current Event Episode Quality artifact.
- `/production-readiness` exposes the canonical, authenticated, fail-closed
  production projection.

## Current economics

Frozen Delta benchmark, 19,521 trades:

| Metric | Result |
|---|---:|
| Average gross | -1.0074 bps/trade |
| Canonical round-trip cost | 14.8 bps/trade |
| Average net | -15.8074 bps/trade |
| Total net | -308,576.93 bps |
| Profit factor | 0.0690 |
| Positive markets | 0 |

This proves the old scalpers were not rejected by a single exit or fee bug.
Their signed gross return was already negative before costs.

## Authority state

- Paper lanes online: **0**
- Legacy manifest candidates blocked: **6**
- Production order route: **absent**
- `can_trade`: **false**
- `can_promote`: **false**

## Remaining evidence blockers (not software defects)

- AMF v3 remains a sparse swing hypothesis with 15/60 required observations.
- The event continuity epoch has not reached its declared duration/event target.
- Event episodes have not demonstrated positive matched-control uplift.
- No strategy has a valid signed paper-eligibility envelope plus canonical
  `authority.paper=true`.

Those blockers must be satisfied by new causal observations and positive
after-cost evidence. They must not be bypassed by UI state or configuration.
