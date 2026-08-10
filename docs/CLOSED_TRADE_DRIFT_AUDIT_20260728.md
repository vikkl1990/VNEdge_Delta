# Closed Trade Drift Audit - 2026-07-28

Scope: read-only audit of `logs/paper_trials/*.journal.jsonl`,
`*.fills.jsonl`, and `research/live_research/paper_lane_performance_latest.json`
on the VM.

## Live Snapshot

- 50 closed paper trades across 26 lanes with closed-trade evidence.
- Fleet paper performance: `-$124.90` net after fees, `$30.55` fees.
- 3 lanes were positive but under-sampled; 12 lanes were negative after fees.
- 0 unpaired closing fills were found in the live fill ledgers.
- Several lanes still had open entry fills, so fleet net includes entry-fee drag
  before those trades resolve.

## Findings

1. The closed-trade reconstruction itself is not corrupt. Live ledgers had no
   unpaired closes in the audited sample.
2. `paper_lane_performance` undercharged per-trade PF/bps by subtracting only
   the exit fill fee from each closing trade. Fleet net subtracted all fees, so
   lane PF/bps could drift from the closed-trade journal and overstate edge.
3. The dashboard closed-trade table displayed `$0.00` fees for actual paper
   trades because actual rows carry `fee_usd`, while the UI read `fees_usd`.
4. The journal KPI blended actual paper closed trades and virtual shadow
   outcomes into one "closed trades" number. That made paper PnL and shadow
   evidence look like the same surface.
5. Open entry fills are legitimate state, but they need an explicit drift flag:
   they drag net PnL through entry fees while the trade is not yet closed.

## Fix

- Compute lane PF and average closed-trade bps from paired entry->exit fills and
  charge both entry and exit fees.
- Publish `closed_net_pnl_usd`, `open_fill_count`,
  `open_position_entry_fees_usd`, `unpaired_closing_fills`, and
  `journal_drift_flags` in paper-lane performance.
- Split journal summary into `actual_closed_trades` and `shadow_closed_trades`.
- Show paper closed net separately from shadow virtual net.
- Fix actual closed-trade fee display in the dashboard table.

## Operator Reading

The losing paper trades are real evidence, not a display artifact. The drift
was in how the cockpit summarized and labeled those trades. After this fix, the
operator should see the fee wall and open-position drag explicitly instead of
having them hidden inside inconsistent PF, bps, and fee fields.
