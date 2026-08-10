# Public Indicator Uplift Audit

`public_indicator_uplift_v1` is the repeatable audit for WillyAlgo/Lux-style
public indicator concepts. It does not copy Pine, TradingView scripts, or
commercial logic. It maps public descriptions into VNEDGE-owned causal atoms and
records what should be mined next.

## Run

```bash
python -m vnedge.research.public_indicator_uplift
```

Default output:

- `research/live_research/public_indicator_uplift_latest.json`
- `research/live_research/public_indicator_uplift_feed.jsonl`

The report is research-only:

- `can_trade=false`
- `can_promote=false`
- untouched judgment and human approval still required

## Current Reading

Most Willy concepts are already represented by VNEDGE atoms inside
`quant_signal_pack_v1`, `alpha_stack_confluence_v1`,
`trend_retest_v1`, and `alpha_distillation_pack_v1`. The missing edge is not
"more indicators"; it is measuring whether specific concept upgrades improve
after-fee expectancy on a lane.

Highest-value uplifts:

1. Stateful FVG/order-block/liquidity lifecycle with age and mitigation.
2. Volume-weighted S/R reaction quality and room-to-next-zone.
3. Fib/harmonic levels as context tags, not standalone entries.
4. Adaptive fresh-flow versus late-flow exhaustion split.
5. Breakout target-room and false-break quality gates.
6. Live exit ladder: partial TP, breakeven, trailing runner.

## Why This Matters

Manual TradingView users can accept visual discretion. The bot cannot. Every
public concept must become:

```text
causal feature -> after-fee backtest -> untouched judgment -> shadow/paper
evidence -> promotion gate
```

Anything short of that remains an idea, not a lane.
