# Scalper Lane Filters

`vnedge.research.scalper_lane_filters` is a Freqtrade-style filter chain for
scalper research lanes. It decides whether an exchange/symbol lane is worth
spending scout, mining, recorder, or replay budget on.

It does not create signals and it never grants trade permission.

## Filter Chain

Default filters run in this order:

1. `recorder_coverage` - tick/L2 stream exists, spans enough time, and has both
   book and trade events.
2. `volume` - public trade count and notional are large enough.
3. `spread` - p95 spread fits the scalper fee wall.
4. `depth` - visible top-of-book depth is not too thin.
5. `precision` - observed price step is not too coarse.
6. `volatility` - recent realized range is neither dead nor disorderly.
7. `replay_state` - known replay tombstones block wasteful remine work.
8. `shadow_performance` - mature negative shadow evidence blocks promotion work.

Missing replay or shadow evidence is a warning by default, not a blocker. That
keeps discovery alive while still exposing why a lane cannot be promoted.

## Output States

- `FILTER_PASS`: every enabled check passed.
- `FILTER_WARN`: no blocker, but optional replay/shadow/precision evidence is
  missing or under-sampled.
- `FILTER_BLOCK`: at least one filter failed. `primary_blocker` names the first
  failed filter in the chain.

Every decision carries:

- per-filter pass/warn/block checks
- metrics and configured thresholds
- `can_trade=false`
- `can_promote=false`

## Fast Scout Integration

`fast_l2_scout` now applies the lane filter before `mine_events`.

Filtered lanes remain visible in `l2_scout_latest.json`:

```json
{
  "state": "FILTERED_LANE",
  "filter_decision": {
    "state": "FILTER_BLOCK",
    "primary_blocker": "spread"
  },
  "can_trade": false
}
```

That means a silent scout cycle can now distinguish:

- missing recorder data
- low volume
- wide spreads
- thin book depth
- coarse tick precision
- dead/disorderly volatility
- replay-tombstoned lanes
- negative mature shadow evidence

The next builds should reuse the same filter evidence in the heavy L2 research
loop, Alpha Council, and Operator Cockpit so lane silence is explained once and
shown everywhere.
