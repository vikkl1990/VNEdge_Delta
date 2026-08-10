# Investing Algorithm Framework Adaptation Notes

Reviewed source: `coding-kitties/investing-algorithm-framework`.

## Useful Ideas Adopted

IAF's strongest ideas for VNEDGE are workflow separation and a searchable
backtest evidence layer: rank/filter before spending heavy scanner CPU, then
store every replay result in a shape the operator can query quickly.

VNEDGE now has a research-only factor ranker that scores every configured
exchange/symbol/timeframe lane using closed-candle data:

- liquidity: recent average quote volume
- tradable range: ATR room over the fee wall
- trend quality: directional momentum weighted by efficiency ratio
- freshness: latest candle age
- coverage: available rows versus configured lookback

The output is written to `research/live_research/factor_ranker.json` and folded
into `research/live_research/latest.json` as `factor_ranker`.

VNEDGE also has `research_evidence_index_v1`, a lightweight adaptation of IAF's
tiered evidence-store idea. It normalizes Pine Lab, scanner tournament,
scanner-uplift, Alpha Arena, fee-wall forensics, contract-matrix, and replay
artifacts into:

- `research/live_research/evidence_index_latest.json` for the dashboard
- `research/live_research/evidence_index.sqlite` for fast local queries
- `research/live_research/evidence_index_feed.jsonl` for append-only history

The index does not replace walk-forward gates or burn-registry judgment. It is
an operator truth table for "what has evidence, what is sparse-positive, what
actually clears the fee wall, and what failed by mode/source."

In production Compose, `evidence-index-publisher` refreshes the JSON, SQLite,
and feed artifacts on `EVIDENCE_INDEX_INTERVAL_SECONDS` cadence after the Pine,
scanner, Arena, fee-wall, and replay producers publish their own reports.

## Safety Boundary

The ranker is not a strategy and not a promotion gate. Its payload carries:

- `research_only=true`
- `can_trade=false`
- `can_promote=false`
- `uses_only_closed_candles=true`

It answers one operational question: "which lanes deserve scanner attention
right now, and why are the others quiet?"

The evidence index has the same boundary:

- `can_trade=false`
- `can_promote=false`
- `live_orders_enabled=false`
- strict fee-wall breakers still require causal port, fee-aware replay, and
  untouched-window judgment before any paper/live promotion.

## Deferred Ideas

These IAF concepts are useful but should be built only after the scanner funnel
has enough evidence:

- Permutation testing for promoted candidates after the normal walk-forward
  gates, not instead of them.
- Parameter bundle archival for large exploratory sweeps.

## Ideas Not Adopted

VNEDGE should not adopt a generic live deployment wrapper from IAF. Execution
continues to run only through the existing VNEDGE gateway, mode ladder,
journal, reconciliation, and kill-switch invariants.
