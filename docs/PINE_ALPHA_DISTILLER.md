# Pine Alpha Distiller

`pine_alpha_distiller_v1` is the source-backed bridge between the Pine Research
Lab and VNEDGE strategy research.

It does not copy Pine into the bot, and it does not make scripts tradable. It
reads local, lawful Pine source artifacts, records only hashes/metadata, and
distills each script into VNEDGE-owned primitive families and replay tasks.

## What It Produces

- Normalized primitives: liquidity zone, sweep/reclaim, range breakout,
  trend trail, momentum confirmation, volume participation, risk plan, MTF
  bias.
- Risk flags: lookahead, unconfirmed `request.security`, last-bar display
  state, visual-only overlays, fixed-session assumptions, missing machine alert
  contract.
- Port tasks: `fvg_liquidity_breakout_v1`, `range_expansion_breakout_v1`,
  `trail_exit_lab_v1`, `orderflow_proxy_v1`, `trend_momentum_context_v1`, and
  `edge_model_feature_bank_v1`.

## Run

```bash
python -m vnedge.research.pine_alpha_distiller \
  --kb research/pine_scripts/pine_research_kb.json \
  --source-dir research/pine_scripts/sources \
  --out research/live_research/pine_alpha_distiller_latest.json
```

Run only the portable subset:

```bash
python -m vnedge.research.pine_alpha_distiller --portable-only --no-write
```

## Promotion Rule

Every output row is research-only:

- `can_trade=false`
- `can_promote=false`
- no Pine source code is emitted
- no runtime strategy is created

Before a distilled family can enter paper or shadow, VNEDGE must implement a
causal Python port and prove:

- expected net edge greater than 25 bps after fees, slippage, and safety buffer
- profit factor greater than 1.5
- at least 20 historical trades
- multi-timeframe replay across the relevant 5m, 15m, 1h, and 4h lanes
- untouched-window judgment
- human approval

## Why This Matters

The TradingView catalog is full of appealing panels and labels. The distiller
turns the accessible source-backed subset into a disciplined work queue:

source review -> primitive extraction -> causality quarantine -> VNEDGE causal
port -> replay -> untouched judgment.
