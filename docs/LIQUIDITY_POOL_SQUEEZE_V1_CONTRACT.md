# Liquidity Pool × Squeeze clean-room v1

This is the one frozen Willy-family interaction experiment. It does not copy
Pine source and does not treat correlated indicator votes as independent edge.

## Testing order

1. `pool_sweep_standalone`: confirmed equal-high/low pool, wick sweep and
   close-back only.
2. `pool_squeeze_full`: standalone event plus prior compression, relative
   volume and opposing-pool target room.
3. Single-gate ablations remove squeeze, volume and target-room individually.
4. Every event is paired to an earlier, outcome-complete control from the same
   symbol, UTC session and causal volatility bucket.
5. Exit optimization remains locked unless the full interaction has positive
   matched-control MFE uplift and wins more than half its pairs.
6. The sealed tail remains closed unless the full interaction separately
   passes frozen selection economics and the matched-control gate.

## Causal definitions

- Decision timeframe: completed 15m candles built from gap-aware 1m data.
- A pivot is unavailable until three right-hand candles have completed.
- Pools need two confirmed pivots within 0.20 ATR and expire after 192 bars.
- Later pivots may update only future state; they cannot rewrite earlier pool
  prices or historical events.
- Sweep: at least 2 bps beyond the level, rejection wick ratio at least 0.40,
  and close back inside.
- Squeeze: Bollinger width was in its causal bottom 20% during one of the prior
  eight completed bars.
- Volume: signal-bar volume is at least 1.25 times the prior 20-bar median.
- Target room: nearest active opposing pool is at least 5 × the 14.8 bps
  taker cost contract away.
- Entry: next completed 15m bar open.
- Stop: 2 bps beyond the sweep extreme; maximum 80 bps.
- Vertical barrier: 16 bars. Stop wins same-bar stop/target ambiguity.

## Safety

The module reads only the selection interval ending at the sealed-tail start.
It cannot auto-open the tail, optimize exits, create a registry lane, paper
trade, promote or trade. AMF v3's 15/60 observations are never consumed or
incremented.

Run:

```bash
python -m vnedge.research.liquidity_pool_squeeze_study \
  --code-version "$(git rev-parse HEAD)"
```

