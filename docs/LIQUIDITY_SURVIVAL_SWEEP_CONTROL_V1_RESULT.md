# Liquidity Survival Sweep Control v1 — first frozen result

Run date: 2026-08-15  
Evidence window: current Event Episode Quality development artifact  
Route cost: `taker_full_14_8` (14.8 bps round trip)  
Holdout: sealed and unopened

## Funnel

| Stage | Count |
|---|---:|
| Independent event episodes | 1,569 |
| Complete market truth + abnormal quality | 33 |
| At a causal merged multi-level location | 3 |
| Passed survival, direction, target-room, and prior-EV gates | 0 |
| Earlier matched controls | 0 |
| Fee-wall outcomes authorized | 0 |
| Exit simulations | 0 |

The three location-qualified observations were BTC continuation failures. Their
available opposing-liquidity room was 22.72, 17.43, and 12.88 bps, versus the
frozen 74 bps minimum. Their realized directional MFE was 11.32, 1.80, and
7.38 bps. None cleared the 14.8 bps route cost even before an exit contract was
tested.

## Verdict

`NO_QUALIFIED_CONTROL_UPLIFT_EXIT_RESEARCH_LOCKED`

The experiment found that stateful liquidity location is selective, but the
currently recorded abnormal episodes still do not contain enough directional
distance for Delta's taker cost structure. This is not authorization to loosen
the distance, probability, or control gates. There is no lane enrollment, no
sealed-tail opening, no exit optimization, and no paper/live authority.

AMF v3 remains an independent swing evidence stream and contributed no samples
to this result.
