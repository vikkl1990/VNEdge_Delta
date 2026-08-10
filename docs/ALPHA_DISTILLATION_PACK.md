# Alpha Distillation Pack

`alpha_distillation_pack_v1` is the research lane that turns public
indicator-stack ideas into VNEDGE-native alpha atoms. It does not copy
WillyAlgo, LuxAlgo, or TradingView/Pine logic. It translates the ideas traders
use manually into causal features that can be measured after fees.

## What It Distills

The inventory starts with the 35 WillyAlgo public concepts and adds LuxAlgo
profile/library primitives as broad, causal research concepts. They are grouped
into twelve atoms:

- `liquidity_sweep`
- `fvg_retest`
- `order_block`
- `squeeze_release`
- `vwap_reclaim`
- `structure_break`
- `trend_trail`
- `profile_reclaim`
- `momentum_impulse`
- `oscillator_divergence`
- `net_volume_flow`
- `activity_zone_reclaim`

The important shift is that these are not standalone trade buttons. Each atom
is evidence in a scored setup:

```text
4h / 1h context
    -> 15m setup atoms
    -> 1m trigger confirmation
    -> expected edge bps versus maker/taker cost
    -> orthogonality and regime permission checks
    -> smart stop/target quality
    -> walk-forward verdict
```

## LuxAlgo Profile Pass

The TradingView LuxAlgo profile currently advertises hundreds of published
scripts; the useful extraction for VNEDGE is not copying those scripts. The v2
atoms translate public Lux-style ideas into testable state:

- `oscillator_divergence`: RSI-style divergence/overflow as exhaustion or veto
  evidence.
- `net_volume_flow`: candle-causal signed-volume participation confirmation.
- `activity_zone_reclaim`: reclaim/rejection around the last high-activity
  price node.
- `orthogonality_score`: requires evidence from multiple roles instead of a
  stack of redundant trend labels.
- `regime_permission`: blocks hostile trend/volatility states before an intent
  exists.

## Fee-Wall Rule

The strategy will not emit a backtest intent unless the setup's estimated edge
clears the configured maker-first floor. Taker eligibility is explicitly
marked in the reason string, but a taker route still needs walk-forward,
untouched judgment, and human approval before any paper/shadow promotion.

The backtester remains conservative: entries and exits still use the existing
taker/slippage cost model. The route label is research telemetry, not an order
instruction.

## Smart Exit

Each signal uses a wick/structure anchored stop plus an ATR guard. The target R
is increased only when the setup score is stronger, bounded by
`max_take_profit_r`. Stops that are too tiny or too wide are blocked before the
intent exists.

The v2 metadata also records a proposed trailing ATR and breakeven-R threshold
in the signal reason. This is research telemetry until partial exits/trailing
orders are wired into the live order manager.

## Run It

```bash
python -m vnedge.research.alpha_distillation \
  --data-root data \
  --out research/live_research/alpha_distillation_latest.json
```

Single lane:

```bash
python -m vnedge.research.alpha_distillation \
  --candidate 'binanceusdm|SOL/USDT:USDT|liquidity_sweep,fvg_retest|long'
```

Diagnostic only, if 1m trigger coverage is incomplete:

```bash
python -m vnedge.research.alpha_distillation --no-1m-trigger
```

## Governance

The report is always research-only:

- `can_trade=false`
- `can_promote=false`
- `requires_untouched_judgment=true`
- `requires_human_approval=true`

A PASS means "pre-register an untouched judgment window." It is not paper,
shadow, or live approval.
