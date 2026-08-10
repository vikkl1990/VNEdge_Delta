# Promotion verdicts (pre-registered, untouched-data judgments)

Only strategies that PASS a pre-registered judgment on data untouched by any
prior decision become eligible for a paper trial (human approval still
required after). This log is the audit trail.

## funding_mean_reversion_v1 — BTC — PASS (2026-07-03)

Untouched window BTC 1h 2024-07-03 → 2025-07-03. Sparse gates. 7/7 windows
traded, 31 OOS trades, +$16.00, 57% profitable windows. Third consecutive
OOS-positive BTC result across independent slices. → **In paper trial** since
2026-07-03 (funding_mr_btc_v1_20260703).

## volatility_expansion_breakout_v1 — DOGE — PASS (2026-07-04)

Pre-registered config (frozen before the run): grid breakout_bars [48, 96],
train 1440 / test 720 bars, OFFENSIVE_GATES. Untouched window DOGE 1h
2024-07-03 → 2025-07-03 (the rolling lab only ever saw 2025-07→2026-07).

One run: 8 windows, all traded, 31 OOS trades, **+$134.28**, 88% profitable
windows. Passed OFFENSIVE_GATES (PF≥1.25, payoff≥1.8, DD≤12%, ≥15 trades,
win-concentration cap).

HONEST CAVEATS (read before approving paper):
- Returns are LUMPY: windows 4–5 show profit factors in the hundreds/thousands
  — a few violent DOGE pumps carry the result. The win-concentration gate
  passed on aggregate, but per-window the edge is uneven.
- Window 0 LOST (−$18.42). Not universally profitable.
- DOGE is a high-volatility meme coin; a vol-expansion breakout catching a few
  big pumps is plausible but likely REGIME-DEPENDENT (2024-25 pump cycle).
- Offensive/meme breakouts are exactly where overfitting and regime-luck hide.

STATUS: eligible for a paper trial; HUMAN APPROVAL required. If approved, it
gets its own frozen manifest + separate trial, watched for the lumpiness above.
Not live capital regardless — the pre-live checklist still applies.
