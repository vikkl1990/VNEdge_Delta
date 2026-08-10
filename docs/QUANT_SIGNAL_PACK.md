# Quant Signal Pack

`quant_signal_pack_v1` is the VNEDGE implementation of the commercial-style
signal stack operators asked for: Lux/Willy-like concepts translated into
causal, testable bot logic. It does not copy proprietary TradingView/Pine
scripts. It converts the visual ideas into auditable research features.

## Included Families

- `structure_break`: break of structure and change of character proxies.
- `liquidity_sweep`: sweep/reclaim of prior liquidity pools.
- `fvg_retest`: bullish/bearish FVG creation and later retest.
- `order_block`: opposite candle followed by displacement through its range.
- `squeeze_release`: volatility compression followed by volume-backed release.
- `vwap_reclaim`: stretched move reclaiming rolling VWAP.
- `confluence`: fallback when no single dominant family owns the setup.
- Multi-horizon bias: fast/mid/slow EMA alignment plus efficiency ratio.
- Volume impulse and displacement filters.

See `docs/LUXALGO_EDGE_EXTRACTION.md` for the deeper LuxAlgo-derived research
contract. The key extraction is not to clone visual indicators; it is to build
a causal confluence state machine that proves which combinations add
after-fee expectancy.

## Safety Line

The pack emits normal `SignalIntent` objects only after closed-candle
evaluation. Every signal has:

- side
- stop
- target
- reason string listing active features

It does not bypass the risk gateway, journal, order manager, or promotion
machinery. A rolling PASS is still only a candidate. Untouched-data judgment and
human approval remain required before paper/live promotion.

## Research Runtime

The continuous research loop now sweeps `quant_signal_pack_v1` under
`OFFENSIVE_GATES` across the configured universe. Runtime shadow lanes can be
created only through the research-to-shadow manifest, and those lanes remain
`can_trade=false` / `can_promote=false`.

Each emitted reason contains the dominant family. Walk-forward records publish
`family_attribution` with trades, net, PF, payoff, and win rate per family. The
bounded research agent can then propose reviewable variants such as:

- `allowed_families=["liquidity_sweep"]`
- `allowed_families=["fvg_retest"]`
- `allowed_sides=["long"]`

These variants are exploratory only. A family-only rolling PASS is still a
candidate for untouched-data judgment, not a promotion.

Primary module:

```bash
src/vnedge/strategy/quant_signal_pack.py
```

Representative tests:

```bash
tests/test_quant_signal_pack.py
```
