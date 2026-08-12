# Failed Auction Response v1 — frozen research contract

Status: **blocked pending event-data readiness**. This document and
`configs/research/failed_auction_response_v1.yaml` define a research hypothesis;
they do not enable a scanner, paper order, or live order.

## Claim

An unusually one-sided aggressive auction that cannot advance price, is met by
displayed replenishment, and is followed by a microprice/flow reversal plus a
failed retest may contain reversal information after Delta India costs.

Raw absorption is not the signal. The existing counterfactual absorption
observer is negative and remains an input diagnostic only. The response after
absorption is the untested hypothesis.

## Data boundary

Official selection is forbidden until a finalized recording window provides:

- BTCUSD and ETHUSD trades plus validated incremental L2;
- at least 14 continuous days and 5,000,000 total events;
- the per-symbol event floors frozen in the YAML;
- at least 95% hourly coverage for every required symbol/channel pair;
- no sequence gaps, checksum failures, duplicate IDs, crash partials, or
  receive-order regressions;
- a verified L2 snapshot before any feature is considered usable; and
- deterministic live/replay parity under the same feature implementation.

An integrity fault invalidates book state. State is reset and no observation is
eligible until a new snapshot validates. Missing depth is never synthesized.
Messages are processed in causal local-receive order; exchange timestamps are
metadata and are never used to move information earlier than receipt.

## Binary setup definition

1. In a 1,200 ms attack window, one aggressor side contributes at least 75% of
   aggressive notional and at least USD 5,000 trades.
2. The auction moves no more than two instrument ticks in its direction.
3. Displayed opposing capacity recovers to at least 70% of its pre-attack size
   within 400 ms.
4. During the next fixed 600 ms, microprice turns at least 0.25 tick toward the
   reversal and at least 60% of new aggressive flow is in the reversal direction.
5. Within 1,500 ms price retests the one-tick level band, does not trade through
   the absorption extreme, and remains valid for 250 ms.

If long and short states overlap, both are rejected. One observation may exist
per symbol. The same level has a 20-second cooldown and a new level must be at
least four ticks away unless the cooldown has elapsed.

## Entry and fill

The decision timestamp is the completion of the failed-retest rule. A fill can
occur no earlier than 100 ms later and never on the decision event. A long uses
the first ask and a short the first bid after latency, with up to 1.5 bps
slippage per leg. The candidate expires after 750 ms. Geometry is recalculated
from the simulated fill and rejected if the book, spread, level, or economics
have become invalid.

## Exit and economics

V1 has one stop, one target, and one fixed 60-second vertical barrier. It has no
partial profit, trailing stop, flow-invalidation exit, regime filter, session
filter, or meta-model.

The stop is beyond the auction extreme by the maximum of three ticks, 1.25
times current spread, and the 95th percentile of two-second event noise. The
only target rule is the opposite boundary of the pre-attack 10-second
micro-range. If this level is absent or not beyond the fill, the candidate is
rejected.

Costs are applied once to realized PnL. The economic gate is stated directly:

```text
net_reward_risk = (gross_target_bps - cost_bps) / (stop_bps + cost_bps)
net_reward_risk >= 1.50
```

The baseline taker/taker cost is 14.8 bps: two 5 bps fees, 18% GST on fees, and
1.5 bps slippage on each leg. This gate has no hidden second cost cushion.

## Kill and untouched rules

Selection requires at least 500 detections, 200 fills, 75 fills in each
chronological half, and 50 per market. It must have positive gross and net
expectancy, average net at least 3 bps, PF at least 1.20, false-signal rate no
more than 70%, positive halves and positive BTCUSD and ETHUSD. No market may
contribute more than 75% of fills.

The final 20% remains sealed unless every selection gate passes. V1 parameters
cannot be swept. A selection failure retires v1; a changed number requires a
new version and a new untouched period.

## Readiness command

Run this only on a stopped or copied recording root whose shards have all been
finalized:

```bash
python -m vnedge.research.failed_auction_readiness \
  --event-root data/delta_events \
  --start 2026-08-11T00:00:00Z \
  --end 2026-08-25T00:00:00Z \
  --code-version "$(git rev-parse HEAD)"
```

Exit status 0 means the **data** contract passed. It still leaves scanner
implementation, selection, paper trading, promotion, and live trading false.
