# LuxAlgo Deep Dive - VNEDGE Edge Extraction

This note extracts public LuxAlgo concepts into VNEDGE-owned, causal research
ideas. It does not copy proprietary scripts or claim LuxAlgo performance. The
goal is to identify the durable trading architecture hidden underneath the
visual toolkits.

Primary sources:

- LuxAlgo TradingView profile: https://in.tradingview.com/u/LuxAlgo/#published-scripts
- LuxAlgo Library: https://www.luxalgo.com/library/
- Smart Money Concept overview: https://www.luxalgo.com/blog/smart-money-concept-indicator-for-tradingview-free/
- Price Action Concepts settings: https://docs.luxalgo.com/docs/algos/price-action-concepts/settings
- Price Action Concepts imbalances: https://docs.luxalgo.com/docs/algos/price-action-concepts/imbalances
- Signals & Overlays intro: https://docs.luxalgo.com/docs/algos/signals-overlays/introduction
- Signals & Overlays modes: https://docs.luxalgo.com/docs/algos/signals-overlays/signals
- Signals & Overlays overlays: https://docs.luxalgo.com/docs/algos/signals-overlays/indicator-overlay
- Signals & Overlays TP/SL: https://docs.luxalgo.com/docs/algos/signals-overlays/tp-sl-points
- Oscillator Matrix library page: https://www.luxalgo.com/library/indicator/luxalgo-oscillator-matrix
- Oscillator Matrix docs: https://docs.luxalgo.com/docs/algos/oscillator-matrix/introduction

## The Unique Winning Factor

LuxAlgo's real advantage is not any single signal. It is **confluence as a
state machine**:

```text
structure event
  + high-value location
  + participation/flow confirmation
  + regime filter
  + execution plan
  + alert/screener/backtest feedback
```

For VNEDGE, the extracted winning factor is:

```text
Only promote a trade candidate when a structural transition occurs at a
pre-identified liquidity/imbalance location, with participation confirming the
move, higher-timeframe context not hostile, and maker/taker economics positive
after fees.
```

This should become a causal **Confluence State Machine**, not a visual
indicator clone.

## LuxAlgo Primitive Map

### Price Action Concepts

Reusable primitives:

- Internal and swing structure: CHoCH and BOS as state transitions.
- Volumetric order blocks: order-block zones plus buy/sell/total volume and
  mitigation rules.
- Liquidity concepts: equal highs/lows, liquidity grabs, trend-line/pattern
  zones.
- Imbalances: FVG, inverse FVG, double FVG / balanced price ranges, volume
  imbalance, opening gaps.
- Premium/discount zones and equilibrium.
- Previous day/week/month/quarter highs and lows.

VNEDGE translation:

- Treat each primitive as a feature with timestamp, direction, age, mitigation
  state, distance-to-price, and forward expectancy.
- Never trade a primitive alone.
- Test whether a primitive improves conditional expectancy after fees.

### Signals And Overlays

Reusable primitives:

- Confirmation signals: trend-following confirmations, including stronger
  trend-aligned variants.
- Contrarian signals: fast reversal/tops-bottoms candidates.
- Overlays: Smart Trail, Reversal Zones, Trend Tracer, Trend Catcher, Neo Cloud.
- Dashboard metrics: trend strength, volatility state, squeeze, volume
  sentiment.
- TP/SL levels: multiple target/stop candidates generated from signal/overlay
  state.
- Alert scripting: machine-readable event composition.

VNEDGE translation:

- Confirmation mode maps to continuation lanes.
- Contrarian mode maps to reversal lanes only when trend exhaustion/overflow is
  present.
- Overlay state becomes a permission matrix: ALLOW, BLOCK, REDUCE_SIZE,
  EXIT_ONLY.
- TP/SL levels become exit-plan metadata until partial exits are live-wired.

### Oscillator Matrix

Reusable primitives:

- Money flow with dynamic thresholds.
- Overflow: excessive one-sided participation, often late-trend exhaustion.
- Hyper-wave / momentum ribbon.
- Real-time divergences.
- Reversal signals.
- Confluence zones / confluence meter.

VNEDGE translation:

- Add participation quality: "is this move supported by fresh flow, or is it a
  late overflow likely to mean-revert?"
- Use divergence/overflow as a veto or exit accelerator, not as a standalone
  entry.
- Mine oscillator-state splits in research: trend-following families should
  improve when flow is aligned; reversal families should improve when overflow
  and divergence appear.

## What We Already Have

VNEDGE already covers part of the Lux-style stack:

- `quant_signal_pack_v1`: liquidity sweeps, FVG retests, order-block proxies,
  squeeze release, VWAP reclaim, structure break, multi-horizon bias.
- `alpha_factory`: forced flow, absorption, microprice dislocation, liquidity
  vacuum, volatility impulse.
- `event_scalper_alpha_tournament_v1`: ranks event families after fees.
- `external_tradingview_signal_v1`: safe intake for third-party TradingView
  JSON alerts, blocked by default until VNEDGE verifies source edge.
- Risk gateway, journal, loss-streak gate, route decisions, and replay gates.

## Addressed 2026-07-07 (retest quality pack)

`trend_retest_v1` implements the WillyAlgoTrader Liquidity Trail Matrix
concepts as a causal research strategy: 4-band ratcheting ATR stack with
self-referential trend flips, 5-factor retest quality score (depth / reclaim
CLV / volume / bias / trend age), SATS-style efficiency gate, wick-anchored
stops, optional strong-pool sweep bonus. `strategy/volume_profile.py` adds
range-distributed trend-segment profiles (POC / VA / HVN / LVN) exposed as
features. All research-only under OFFENSIVE_GATES.

## Implemented Profile Pass

`alpha_distillation_pack_v1` now includes a LuxAlgo profile pass that extracts
the public-script/library lesson into causal atoms:

- `oscillator_divergence`: RSI-style divergence/overflow evidence.
- `net_volume_flow`: signed candle-volume confirmation.
- `activity_zone_reclaim`: reclaim/rejection around the last high-activity
  price node.
- `orthogonality_score`: forces setup evidence to come from distinct roles
  such as location, structure, trend, momentum, and participation.
- `regime_permission`: blocks hostile higher-level trend/volatility states
  before a signal can become an intent.
- Exit telemetry: proposed trailing ATR and breakeven-R metadata are attached
  to reasons for later partial-exit wiring.

## What Is Missing

The gaps that matter most:

1. **Stateful imbalance lifecycle**
   - Current pack has FVG retests.
   - Missing: inverse FVG, double FVG / balanced price range, mitigation method,
     zone age, zone quality, and distance-to-zone.

2. **Volumetric order-block quality**
   - Current pack has an order-block proxy.
   - Missing: buy/sell volume split, total volume, percentage dominance,
     mitigation state, and mid-line reaction.

3. **Liquidity-pool pressure** — PARTIALLY ADDRESSED 2026-07-07
   (`strategy/liquidity_pools.py`): equal high/low clustering via ATR
   tolerance, 4-factor pool strength (touches / recency half-life / volume /
   HTF bonus), wick-pierce + close-back mitigation lifecycle, pivot
   confirmation lag honoured. Still missing: prior day/week/month levels.

4. **Overlay/regime permission matrix**
   - Current pack has EMA/ER bias.
   - Missing: explicit trend strength, squeeze state, volatility warning,
     reversal-zone proximity, and trend-overlay agreement score.

5. **Money-flow overflow**
   - Current pack uses volume z-score.
   - Missing: late-flow overflow/exhaustion feature that can veto continuation
     and activate reversal lanes.

6. **Confluence decay**
   - Current scoring is mostly same-bar.
   - Missing: age-weighted confluence. A fresh CHoCH at an unmitigated FVG is
     different from a stale FVG touched five times.

7. **Live partial exits**
   - External/Willy-style plans carry TP splits as metadata.
   - Missing: live order-manager support for partial TP, breakeven promotion,
     and runner trailing.

## VNEDGE Winning Formula

The bot should not ask "did a Lux-style signal fire?"

It should ask:

```text
Is there a fresh structural transition,
at a high-value liquidity/imbalance location,
with confirming participation,
inside a non-hostile higher-timeframe regime,
and does the after-fee route gap clear maker/taker economics?
```

Candidate score:

```text
confluence_score =
  structure_transition_score
  + location_quality_score
  + participation_score
  + regime_permission_score
  + execution_route_gap_score
  - staleness_penalty
  - hostile_context_penalty
```

Promotion rule:

```text
score is not enough.
The confluence bucket must beat the same no-confluence bucket OOS,
after fees, across untouched replay/walk-forward data.
```

## Scalper-Specific Extraction

For scalping, the Lux-style edge cannot be candle-only. The candle concepts
should select **where** to look, while L2 decides **whether to enter**.

Best scalper lane:

```text
HTF liquidity/imbalance magnet
  -> price enters zone
  -> L2 forced flow / absorption / liquidity vacuum confirms
  -> maker route clears fee wall
  -> conservative replay approves
```

This is different from our tombstoned book-imbalance family. The failed family
looked only at continuous top-of-book imbalance. The winning Lux extraction is
**location-conditioned microstructure**.

## Build Order

1. Add causal Lux-state columns to `quant_signal_pack_v1`:
   - inverse FVG
   - double FVG / balanced price range
   - mitigation state and age
   - equal high/low liquidity pools
   - prior day/week/month levels
   - premium/discount/equilibrium

2. Add confluence-state attribution:
   - which state fired
   - whether it was fresh/stale
   - whether it was mitigated/unmitigated
   - whether HTF context was aligned/mixed/hostile

3. Feed those states into `edge_leaderboard` and `alpha_factory.tournament`.

4. Add L2 zone-conditioned replay:
   - only mine forced-flow/absorption/vacuum events inside active liquidity or
     imbalance zones.

5. Only after replay evidence:
   - consider partial-exit live wiring.

## Bottom Line

LuxAlgo's unique lesson for us is:

```text
Do not build more indicators.
Build a confluence state machine that proves which combinations actually add
after-fee expectancy.
```

Our edge is not being a Lux clone. Our edge is combining Lux-style confluence
with VNEDGE's stricter replay, route, fee-wall, and risk-gateway machinery.
