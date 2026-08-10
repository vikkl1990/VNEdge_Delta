# Maker Quote Lifecycle

`maker_quote_lifecycle_v1` is the missing execution-truth layer between a
scanner signal and paper/live readiness.

The report answers one question:

> Did this lane actually prove a post-only maker quote path, or is it only
> producing chart signals that still lose to fees?

## What It Reads

- `logs/paper_trials/*.journal.jsonl`
  - `executor_started`
  - `executor_route_check`
  - `executor_maker_submitted`
  - `executor_taker_submitted`
  - `executor_finished`
  - `executor_scalper_risk_decision`
- `research/live_research/paper_lane_performance_latest.json`
- `research/live_research/paper_trade_exit_autopsy_latest.json`

It is read-only. It cannot trade, promote, demote, restart services, or mutate
lane config.

## States

| State | Meaning | Operator Action |
|---|---|---|
| `NO_QUOTE_LIFECYCLE_WIRING` | Lane has paper/research rows but no maker/taker executor events. | Wire fired signals through `MakerTakerExecutor` before judging execution. |
| `COLLECT_MAKER_QUOTE_SAMPLE` | Executor is present, but there are too few quote attempts. | Keep observing. |
| `MAKER_ROUTE_BLOCKED` | Maker route failed before quote submit. | Fix route cost/edge assumptions. |
| `MAKER_FILL_UNPROVEN` | Maker quotes are posted but fills are too weak or absent. | Collect/repair queue and fill telemetry. |
| `TAKER_FALLBACK_FORBIDDEN` | Taker fallback correctly refused because edge does not pay fees. | Keep maker-only; do not chase. |
| `TAKER_FALLBACK_NEEDS_AUTOPSY` | Taker fallback is used but closed-trade evidence is immature. | Run execution autopsy before promotion. |
| `NEGATIVE_AFTER_EXECUTION` | Closed paper path is negative after costs. | Return lane to research. |
| `TIMEOUT_UNKNOWN_FAIL_CLOSED` | Executor has unresolved order state. | Fail closed and reconcile. |
| `MAKER_OBSERVED_SHADOW_READY` | Maker lifecycle exists, but proof is not promotion-grade. | Continue observation/replay. |
| `QUOTE_LIFECYCLE_PAPER_REVIEW` | Maker lifecycle plus mature positive paper proof exists. | Human review only; normal promotion gates still apply. |

## Taker Rule

Taker fallback is never a convenience route. It is only acceptable when the
remaining expected move clears:

- minimum taker net edge, default `25 bps`
- minimum taker cost coverage, default `1.5x`

If either fails, the correct behavior is no trade.

## VM Service

The Docker service is:

```bash
maker-quote-lifecycle
```

Default output:

```bash
research/live_research/maker_quote_lifecycle_latest.json
research/live_research/maker_quote_lifecycle_feed.jsonl
```

Dashboard endpoint:

```bash
/maker-quote-lifecycle
```
