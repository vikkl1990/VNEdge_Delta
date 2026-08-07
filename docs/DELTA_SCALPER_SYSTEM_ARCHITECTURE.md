# VNEDGE Delta India Scalper Engine — System Architecture

For the peer-review inventory of every implemented module, runtime path,
research program, safety boundary, current evidence, and known gap, see
[Implemented Architecture and Code Flow](DELTA_SCALPER_PEER_REVIEW.md).

Version 1.0, implemented 5 August 2026.

## Deployed topology

```mermaid
flowchart TD
    Delta["Delta India public REST + WebSocket"] --> Ingest["Async ingestion, normalization, sequencing, gap recovery"]
    Ingest --> Candles["Closed multi-timeframe candle store"]
    Ingest --> Flow["L2 and trade-flow store"]
    Candles --> Context["Context builder<br/>indicators, regime, trend, volatility,<br/>session, CUSUM alarm, bars-since-shift,<br/>shift-age bucket, return and vol scores"]
    Flow --> Context
    Context --> Scanners["Versioned scanner hypotheses<br/>all currently rejected and disabled"]
    Scanners --> Signal["Causal signal gates and exit plan<br/>Fee model applies Delta India round-trip costs<br/>to every candidate and produces expected net bps"]
    Signal --> Journal["Exactly-once research journal"]
    Signal --> Forward["Next-bar orderless forward outcomes"]
    Signal -. "adapter available, not invoked" .-> Risk["Existing VNEDGE risk gateway"]
    Risk -. "blocked until promotion" .-> Orders["OrderManager and paper broker"]
    Journal --> Dashboard["Authenticated local dashboard"]
    Forward --> Dashboard
```

The deployed service is one asyncio process, but it is a research sidecar. It
does not instantiate an account client, `OrderManager`, or broker. Describing
it as already embedded in the main execution kernel would be inaccurate.

## Closed-candle signal sequence

```mermaid
sequenceDiagram
    participant WS as Delta public WS
    participant Store as Candle store
    participant Ctx as Context builder
    participant Scan as Scanner engine
    participant Gate as Signal and fee gates
    participant WAL as Research journal
    participant Fwd as Forward tracker
    WS->>Store: completed 1m or 5m candle
    Store->>Store: reject duplicate, future, or regressing close
    Store->>Ctx: read immutable closed snapshots
    Ctx->>Scan: features, regime, funding, L2 confirmation
    Scan->>Gate: zero or more complete candidates
    Gate->>Gate: cost, probability, confidence, symbol and dedup gates
    Gate->>WAL: journal decision exactly once
    alt candidate accepted
        Gate->>Fwd: register observation
        Fwd->>Fwd: enter at next 1m open - resolve stop-first
        Fwd->>WAL: MFE, MAE, expected and realized net bps
    else candidate rejected
        Gate->>WAL: journal rejection reasons
    end
```

## Live/replay parity

`build_delta_scalper_assembly()` is the single construction path for the
context builder, regime engine, move predictor, scanners, fee model, and final
gates. Both the live shadow service and offline replay load the same strict
YAML configuration and call that factory. Historical replay omits L2 because
candle history cannot reconstruct an event-level book; live L2 remains an
attached confirmation field and never changes the candle trigger.

## Current component status

| Layer | Implementation | Status |
|---|---|---|
| Data | Public WS, REST backfill, candle gaps, sequence checks | Active |
| Intelligence | Shared features, regimes, hierarchy context, legacy predictor | Active research support |
| Decision | Three assembly scanners plus one paired research hypothesis, costs, ranking, exits | Available; all rejected and disabled |
| Research | Causal replay, untouched split, fee sensitivity, forward outcomes | Active |
| Control | Strict YAML, snapshots, journal, authenticated dashboard | Active |
| Existing risk core | Risk adapter using the existing gateway | Available, not invoked |
| Execution | OrderManager, account stream and broker | Not constructed |

## Promotion boundary

The supplied diagram's final happy-path step—submission to the existing risk
gateway—is a future paper-mode boundary, not current behavior. The 2025-to-date
untouched evidence fails the configured profitability and data-quality gates.
Consequently the machine-readable architecture manifest and dashboard enforce
`order_route_present=false`, `can_trade=false`, and `can_promote=false`.

## Detailed stage ownership

The supplied sequence places fee and move enrichment after scanner evaluation.
In the implemented interface, legacy scanners ask the shared deterministic
`MovePredictor`; the hierarchical scanner uses an explicitly uncalibrated,
non-promotable structural prior. Every scanner asks `DeltaFeeModel` while
constructing its complete immutable candidate. The signal generator then
applies global gates, ranking, and dedup. This keeps every candidate
self-describing and makes replay/live candidates identical.

L2 and CVD are attached to scanner outputs as confirmation metadata. They
are not consulted by the entry predicates, even for Imbalance Fade. This is an
intentional correction to the supplied diagram and preserves causal replay
parity when historical event-level books are unavailable.

## Error handling and edge cases

| Condition | Behavior |
|---|---|
| Duplicate or regressing candle | Rejected before evaluation |
| Missing candle interval | Pause that stream, REST backfill, resume next close |
| Context construction error | Fail closed, journal typed rejection |
| One scanner raises | Record typed scanner error; continue other scanners |
| No candidate | Journal normal no-selection decision |
| Duplicate candidate key | Suppress and record duplicate reason |
| Journal unavailable | Remove selected candidate; no forward route |
| L2 stale or sequence unhealthy | Mark confirmation status; never trigger execution |
| Stop and target in one replay bar | Resolve stop first and flag ambiguity |
| Historical L2 unavailable | Replay candles only and report L2 as unused |

## Timing annotations

Every decision contains monotonic microsecond measurements for context
construction, each enabled scanner, global fee/probability/confidence gates,
ranking/dedup, and total generation time. These values are observability only:
they never enter features, ranking, or replay results. The latest trace and WAL
write status are available in the authenticated `/delta-scalper` response.

## Equivalent research replay flow

```mermaid
sequenceDiagram
    participant Data as Historical closed 1m candles
    participant Store as Shared candle store
    participant Engine as Shared live/replay assembly
    participant Pending as Pending candidate
    participant Path as Conservative path simulator
    participant Report as Evidence report
    Data->>Store: append one closed 1m candle
    Store->>Store: causally aggregate 5m, 15m, 1h and 4h
    Store->>Engine: run identical context, scanners, predictor, fees and gates
    alt accepted candidate
        Engine->>Pending: wait - do not fill on decision candle
        Pending->>Path: enter at next 1m open
        Path->>Path: update MFE MAE with stop-first ambiguity rule
        Path->>Report: exit price, hold time, realized costs and net bps
    else rejected or no candidate
        Engine->>Report: count evaluation without a trade
    end
    Report->>Report: daily weekly monthly quarterly and untouched validation
```
