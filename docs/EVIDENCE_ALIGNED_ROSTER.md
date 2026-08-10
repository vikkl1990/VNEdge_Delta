# Evidence-aligned shadow roster

The Delta-India shadow roster is realigned to what an **independent, position-aware
backtest** actually supports, replacing the 5m fee-wall scanner lanes.

## Why

`research/second_eye_grid.py` runs every registered strategy through the real
`run_backtest` (sequential single position, taker fills, adverse slippage) — a
separate code path from the fee-wall opportunity-scanner that seeds the live
lanes. Over the full exchange × symbol × timeframe grid it found:

- **The edge is at 4h/1h, not 5m.** 27 of 44 honest survivors (n≥20, PF≥1.3,
  net>0) are 4h; only 3 are 15m.
- The deployed **5m Delta scanner lanes are zero-trade or net-negative** at
  honest taker fees (e.g. `sats_5m` SOL 5m PF 0.64, XRP 5m PF 0.41).
- The strongest, cross-venue-consistent cell is **`vnedge_algo_ml_pro` ETH 4h**
  (PF ~3.6, 77% win, DD ~$6 on all three exchanges) — and it wasn't deployed at
  all.

## What changed

- **New:** `evidence_aligned_shadow_lanes()` enrolls the Delta-India grid
  survivors as SHADOW lanes with default params (so each lane matches exactly
  what the backtest measured):

  | strategy | symbol | tf | grid PF | n |
  |---|---|---|---|---|
  | vnedge_algo_ml_pro_v1 | ETH | 4h | 3.64 | 22 |
  | vnedge_algo_ml_pro_v1 | DOGE | 1h | 1.54 | 128 |
  | stealth_trail_bbp_v1 | ETH | 4h | 1.79 | 49 |
  | luxy_ut_bot_forecast_v1 | XRP | 1h | 1.66 | 44 |
  | quant_signal_pack_v1 | BNB | 4h | 1.65 | 39 |

- **Wiring:** `vnedge_algo_ml_pro_v1` is now constructible in the multi-lane
  runtime (it was registry-only before).
- **Disabled by default in `docker-compose.yml`** (deployment layer, reversible):
  `MULTI_LANE_SATS_5M_DELTA`, `MULTI_LANE_STEALTH_TRAIL_BBP_DELTA`,
  `MULTI_LANE_FVG_LIQUIDITY_DELTA`, `MULTI_LANE_LUXARA_LIVE_PLAN_DELTA`,
  `MULTI_LANE_LUXARA_BREAK_BOUNCE_DELTA`. Set any to `1` to restore that group.
  Toggle the whole survivor set with `MULTI_LANE_EVIDENCE_ALIGNED` (default `1`).

## Live-forward paper trial (the promotion test)

Instead of a historical walk-forward, the top candidate is validated by a
**live-forward PAPER trial** — `evidence_paper_trial_lanes()` runs
`vnedge_algo_ml_pro` on live Delta data in PAPER mode: simulated fills through
the full risk gateway (real fees / funding / slippage, paper capital). This is
"real-money-equivalent, paper money", and it's a *stronger* out-of-sample test
than a backtest — forward data is genuinely unseen, so it cannot be overfit and
there is zero lookahead.

Run in parallel so several candidates mature at once (a portfolio read, not a
one-shot on a single strategy):

| strategy | symbol | tf | grid PF | n |
|---|---|---|---|---|
| vnedge_algo_ml_pro_v1 | ETH/USD | 4h | 3.64 | 22 |
| vnedge_algo_ml_pro_v1 | DOGE/USD | 1h | 1.54 | 128 |
| stealth_trail_bbp_v1 | ETH/USD | 4h | 1.79 | 49 |
| luxy_ut_bot_forecast_v1 | XRP/USD | 1h | 1.66 | 44 |
| quant_signal_pack_v1 | BNB/USD | 4h | 1.65 | 39 |

- Requires `paper` in `MULTI_LANE_MODES` (the default). Toggle the whole set with
  `MULTI_LANE_EVIDENCE_PAPER_TRIAL`.
- **Pre-registered pass/fail criteria** are locked, per candidate:
  `vnedge_algo_ml_pro_eth_4h_20260726.yaml` (the crown jewel) and
  `evidence_survivors_20260726.yaml` (the diversification round). Bar: net
  positive after cost, PF ≥ 1.3, ≥ 15 trades over ≥ 30 days, DD ≤ 8%. A PASS
  makes a candidate *eligible* for live — never an auto-promotion.
- `stealth_trail_bbp` carries a caveat: its backtest drawdown ran high, so the
  trial specifically tests whether live DD stays inside the 8% bar; it is also a
  known duplicate of `human_trade_fingerprint_v1` (registry cleanup tracked
  separately).
- **Sample-rate reality:** 4h fires ~2×/week, so a meaningful trial takes
  **weeks, not days**. This is a slow, honest forward test.

## What did NOT change

- **Still SHADOW-only.** No paper promotion, no live orders. Every gate and the
  gateway are untouched.
- **Grid results are single-window** (no walk-forward / OOS) — suggestive, not
  promotion-grade. A positive shadow result still requires the usual
  pre-registered **untouched-window judgment** before any paper or live
  permission. `stealth_trail_bbp_v1` and `human_trade_fingerprint_v1` remain a
  known duplicate to reconcile separately.
