# Indicator Family Scoring v1

This module gives every research candidate an explainable 0–100 quality
score. It is an attribution layer, not a scanner, gate, or execution route.

## Families

| Family | What it measures | Examples |
|---|---|---|
| Structure | Direction and location | HTF bias, VWAP location, BOS/CHOCH/sweep |
| Momentum | Directional persistence | sustained event direction, displacement, score delta |
| Volatility | Whether movement can clear costs | causal ATR percentile |
| Participation | Whether activity supports the move | volume z-score |
| Order flow | Event-tape pressure | book imbalance, trade-flow imbalance, absorption |
| Liquidity | Executability | spread and displayed depth |
| Economics | Whether the geometry pays | target/cost multiple, net expectancy, reward/risk |
| Data quality | Whether evidence is trustworthy | closed bar, sequence/checksum, freshness, feed delay |

The composite is a policy-weighted mean of the families that are actually
available. Coverage is reported separately, so missing L2 or volatility data
cannot masquerade as a strong zero-filled score. Required families fail
closed. Future-dated evidence raises an error.

## Runtime contract

- Every evidence row records its raw value, normalized score, confidence,
  timestamp, weight, and human-readable explanation.
- Hard data-quality failures block research qualification.
- Economics and data quality are always required.
- `research_qualified` means only that the observation cleared this advisory
  score policy. It does not mean profitable, promotable, paper-eligible, or
  tradable.
- Every result permanently reports `research_only=true`, `can_trade=false`,
  `can_promote=false`, `used_for_signal=false`, and
  `used_for_execution=false`.

The frozen policy lives at
`configs/research/indicator_family_scoring_v1.yaml`. Any future use as a hard
gate requires a new version and independent chronological/untouched evidence.

## Historical calibration

Run the causal historical adapter and calibration report with:

```bash
.venv/bin/python -m vnedge.research.indicator_score_calibration
```

The report is written to
`research/live_research/indicator_score_calibration_latest.json` and is shown
on the Delta dashboard. Decile boundaries are learned from the first 80% of
the journal and then applied unchanged to the final 20%.

This archived journal does not contain the participation, order-flow, or
liquidity evidence required by the full live score. Those families are marked
missing rather than reconstructed from future data. The historical tail was
already inspected in earlier research, so this report is diagnostic and is
not a new sealed-holdout proof. The report cannot grant scanner, paper, live,
or promotion authority.
