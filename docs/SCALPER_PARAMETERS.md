# Scalper Parameter Registry

`vnedge.scalping.parameter_registry` is the frozen source of truth for scalper
research parameters. It does not trade or promote anything. It defines what
the L2 research loop is allowed to test.

## Context, Execution, And Horizons

The registry separates context from execution.

Context timeframes:

- `4h`: macro regime / side avoidance
- `1h`: volatility, funding, session context
- `15m`: structure and local pressure zones
- `1m`: setup confirmation / candle context

Execution/replay timeframes:

- event-driven tape
- `250ms`
- `500ms`
- `1s`
- `3s`
- `5s`
- `15s`
- `30s`
- `60s`
- `1m_research_proxy`

The `1m` context lane is human-readable setup context. The
`1m_research_proxy` lane is only a candle approximation for fee-wall testing.
Neither is promotion-quality proof for scalping. True scalper validation is
tick/L2 replay.

## Families

V1 families:

- `forced_flow_continuation`
- `absorption_reversal`
- `microprice_dislocation`
- `liquidity_vacuum_continuation`
- `volatility_impulse`

Each family owns its horizon set, flow thresholds, spread limits, sample
requirements, route gates, and exit policy. Parameter changes should be code
reviewed because they alter the research contract.

Family lifecycle is explicit:

- `active_research`: still eligible for mining, replay, and untouched
  judgment if evidence appears.
- `deprioritized`: kept visible, but not first in the recorder/replay queue.
- `tombstoned`: failed a replay/research verdict and must not be treated as
  an active alpha premise without a new pre-registered idea.

`book_imbalance_continuation` is now `tombstoned`. The 2026-07-05 recorded L2
sweep found all 120 configurations negative after costs across Binance and
Delta lanes. It remains in the registry only as a replay reference and audit
baseline.

## Fees And Route Hurdles

The registry stores maker/taker/slippage/safety-buffer assumptions per venue.
Route gates remain conservative:

- maker route requires positive net bps and PF above the maker floor
- taker route requires enough extra PF/net bps to cover higher entry cost
- below the floor means `BLOCKED`, not a weak signal

## Context Mining

The alpha factory now tags every structural hypothesis with one of:

- `aligned`
- `mixed`
- `hostile`
- `missing`

Those tags are mined as evidence splits. They do not approve, reject, or trade
by themselves. The question they answer is: does the same L2 pattern clear
fees only when `4h/1h/15m/1m` context agrees with the scalp side?

## Exit Intelligence

Current live safety:

- reduce-only exits remain allowed through the gateway
- tick stops are available as risk infrastructure
- replay static exits use stop/target/TTL at tradable touch prices

Smart-exit replay policies:

- `static_fast`: stop + target + TTL; live-wired baseline
- `adverse_cut`: exits early when post-fill adverse selection breaches a
  tighter threshold before the static stop
- `adaptive_trail`: activates a trailing exit after favorable movement and
  can lock partial profit on retrace

Replay diagnostics can compare policies directly:

```bash
python -m vnedge.research.scalper_replay_diagnostics \
  --day 20260705 \
  --family-id liquidity_vacuum_continuation \
  --exit-policy adaptive_trail
```

Replay rows include `exit_policy_id` and `exit_reason_counts`, so smart exits
must prove themselves as actual `adverse_cut` or `trail` closes, not just as
configured intent.

The registry reports an exit-intelligence score. The honest status is:

- live path: developing/static
- replay path: adaptive policies can now be measured
- production scalper: not smart-exit-ready until the hot loop wires the
  chosen policy into reduce-only order generation

## Runtime Visibility

The decoupled L2 loop publishes the registry in:

```text
research/live_research/l2_latest.json.scalper_parameter_registry
```

The hourly candle loop folds the same payload into:

```text
research/live_research/latest.json.scalper_parameter_registry
```
