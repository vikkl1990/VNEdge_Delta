# Momentum Cascade Lyro Scanner

Status: research-only, not paper/live eligible.

This scanner is a VNEDGE-native adaptation of the supplied Momentum Cascade
TradingView concept.  It keeps the causal trading idea: one ROC impulse is
passed through two EMA stages, and a trend only flips when all three stages
agree.  VNEDGE then adds execution-grade filters that the visual indicator
does not supply by itself:

- completed 1h EMA/ER/ADX/cascade bias,
- 15m body impulse and volume participation,
- cascade coherence,
- structure break/sweep/rejection context,
- structural stop and TP1/TP2/TP3 metadata,
- `expectedEdge` and `fillProbability` hints for the maker/taker router.

The implementation does not execute or copy Pine in the trading path.  Signals
still require the normal edge-router, model, untouched-data judgment, shadow,
and paper gates before any promotion.

## VM proof on commit 161ed4c

Data root: `/home/ubuntu/vnedge/data`

Container: `vnedge-research-loop:latest`

Strategy: `momentum_cascade_lyro_v1`

Route policy: research-only, 16-bar horizon, minimum 20 samples, expected net
edge floor 25 bps, PF floor 1.5.

### Raw cascade result

Before tightening, the raw cascade fired often but lost after costs:

| Scope | Routed | Avg selected net | PF | Verdict |
| --- | ---: | ---: | ---: | --- |
| Delta ETH 15m, 30d | 84 / 86 | -19.08 bps | 0.55 | Negative |
| 18-lane 15m universe, 30d | 1,359 | -15.82 bps | 0.59 | Negative |

The model filter did not rescue the raw feed:

| Scope | Opportunities | Model trades | Model avg net | Model PF | Verdict |
| --- | ---: | ---: | ---: | ---: | --- |
| 18-lane 15m universe, 30d | 1,415 | 7 | -99.55 bps | n/a | Under-sampled negative |

### Strict impulse gates

The stricter scanner requires body impulse, coherence, and high expected net
edge.  This reduced noise, but still did not pass robustly:

| Scope | Routed | Avg selected net | PF | Verdict |
| --- | ---: | ---: | ---: | --- |
| 18-lane 15m universe, 30d | 71 | -11.39 bps | 0.77 | Negative |
| SOL + BNB 15m, 30d | 24 | +24.61 bps | 1.60 | Near miss, below 25 bps floor |
| 18-lane 15m universe, 90d | 205 | -5.78 bps | 0.87 | Negative |
| SOL + BNB 15m, 90d | 68 | +3.41 bps | 1.08 | Collapsed |
| Delta ETH 5m, strict | 0 | n/a | n/a | No opportunities |

## Decision

Do not promote this scanner to shadow or paper as a standalone execution lane.
The 15m cascade is visually useful and reduces negative selection when gated
by impulse/coherence, but the edge does not survive a longer window.

The right use is as an alpha feature inside a broader classifier or agent
council feature set:

- cascade stage values: `cascade_m1`, `cascade_m2`, `cascade_m3`,
- full-agreement trend and flip flags,
- side-signed `cascade_m3_slope`,
- cascade coherence,
- impulse-gated candidate state.

Any future promotion attempt must be a new pre-registered round on untouched
data, preferably as a model feature combined with order-flow/liquidity regime
features rather than as a standalone visual-indicator lane.
