# Continuous MTF Alignment v2 — First Selection Result

## Decision

`continuous_mtf_alignment_v2` is rejected and remains research-only. Mechanical
1h BOS/CHOCH reduced frequency but materially worsened both gross and after-cost
expectancy. The untouched period was not opened.

## Frozen change from v1

V2 replaced only the 1h EMA-slope/lookback-break agreement layer with:

- confirmed 1h swing highs/lows using 3 left and 3 right bars;
- an 8 bps minimum excursion from the latest accepted opposite swing;
- trend from the latest 5 accepted swings;
- close-only BOS and CHOCH against the latest swing of the required type;
- one break event per confirmed swing;
- a 24-hour freshness limit for an aligned BOS/CHOCH event.

The 4h bias, 15m setup, 5m confirmation, 1m trigger, next-open entry, structural
target, stop, fee model, and selection boundary were unchanged. This isolates
the economic effect of the mechanical 1h structure layer.

## Selection evidence

Window: 1 January 2025 through the embargoed decision close on 1 April 2026.

| Metric | V1 benchmark | Mechanical v2 |
|---|---:|---:|
| Intents | 421 | 185 |
| Completed trades | 118 | 49 |
| Trades/day | 0.259 | 0.107 |
| Average gross bps/trade | +2.10 | -22.15 |
| Average cost bps/trade | 14.80 | 14.80 |
| Average net bps/trade | -12.70 | -36.95 |
| Total net bps | -1,499.04 | -1,810.51 |
| Profit factor | 0.714 | 0.341 |
| Win rate | 30.51% | 26.53% |
| False-signal rate | 69.49% | 73.47% |
| Positive markets | 0 | 0 |

Market results:

| Market | Trades | Average net bps | Profit factor |
|---|---:|---:|---:|
| BTCUSD | 26 | -28.16 | 0.383 |
| ETHUSD | 23 | -46.89 | 0.310 |

The accepted intent states comprised 126 `aligned_bos` and 59
`aligned_choch` observations. Of 185 intents, 136 were rejected at next-open
geometry: 95 for insufficient reward/risk, 37 for invalid structural geometry,
and 4 for insufficient target-to-cost multiple.

The two accepted BTC breakout trades averaged +28.92 bps, but a two-trade cell
is not evidence and must not be promoted or tuned around. Pullbacks accounted
for 45 of 49 completed trades and remained strongly negative in both markets.

## Gate outcome

Passed:

- data quality;
- market concentration.

Failed:

- minimum 80 completed trades;
- gross expectancy above costs;
- positive net expectancy;
- profit factor at least 1.15;
- at least one positive market;
- false-signal rate below 70%.

## Causality and safety

- All swing levels were visible only after the third right-hand 1h bar closed.
- BOS/CHOCH required a close beyond a confirmed level; wick-only breaks did not count.
- Shared-close updates were applied before 1m evaluation.
- Entry remained the next 1m open with conservative stop-first ambiguity handling.
- Missing source minutes: zero for both markets.
- The sealed interval from 2 April through 25 July 2026 was not read for prices,
  predictions, or trades.
- `can_trade = false`, `can_promote = false`, and no order route exists.

## Verification

- Targeted mechanical structure and continuous-MTF tests: 13 passed.
- Full repository suite: 1,986 passed, 1 third-party deprecation warning.

## Conclusion

Mechanical BOS/CHOCH improves semantic clarity and lowers activity, but it does
not improve the scanner's economics. It selects worse forward paths than v1,
even before costs. V2 is therefore a rejected research artifact; do not open the
untouched tail and do not tune its thresholds on this selection window.
