# Session Liquidity Sweep v1 — First Replay Result

Verdict: **selection failed; untouched 20% remains sealed; v1 retired**.

The contract and implementation were committed before the first historical
result. The selection replay used gap-free BTCUSD and ETHUSD 1m candles and
the causal 08:01 London start defined in the frozen contract.

## Official selection result

| Metric | Result | Required | Pass |
|---|---:|---:|:---:|
| Qualified candle setups | 48 | diagnostic | — |
| Completed trades | 0 | ≥80 | **No** |
| Entry geometry rejections | 48 | — | — |
| Data gaps / unresolved | 0 / 0 | 0 / 0 | Yes |
| Untouched evaluation | Not computed | selection pass first | Sealed |

All 48 setups failed `structural_target_below_cost_multiple`. BTC produced 21
setups and ETH 27; New York produced 35 and London 13. The median next-open
structural target was 16.89 bps, the maximum was 38.25 bps, and the maximum
cost multiple was 2.585. The frozen requirement was 51.8 bps, or 3.50× cost.

Zero official trades means the zero net and false-signal values in the machine
report are empty-sample values, not evidence of profitability.

## Selection-only diagnostic — not part of v1 execution

To determine whether the cost rejection hid a near miss, the 48 rejected
setups were measured over their next 45 minutes without changing v1:

| Counterfactual path | Average net | PF | Win rate | Verdict |
|---|---:|---:|---:|---|
| Trade each setup at its own 1R | −15.10 bps | 0.154 | 27.1% | Failed |
| Fixed 51.8 bps target, structural stop | −17.96 bps | 0.300 | 22.9% | Failed |

Although average 45-minute MFE was 54.49 bps, average MAE was also 52.87 bps.
Stop-first path resolution, rather than terminal excursion, exposes the lack of
tradable edge. New York showed more excursion than London but remained deeply
negative in both counterfactuals.

## Comparison with range-compression v1

Range-compression v1 executed 197 observations and lost 13.84 bps per trade at
PF 0.312. Session-sweep v1 is safer operationally because its cost gate blocks
all 48 setups, but it supplies no trade frequency or positive expectancy. Its
selection-only counterfactuals are no better than the rejected baseline.

This is an improvement in **loss prevention**, not an improvement in alpha.
No threshold relaxation, v1 tuning, meta-model, or untouched evaluation is
authorized. Safety remains `can_trade=false`, `can_promote=false`, with no
order route.
