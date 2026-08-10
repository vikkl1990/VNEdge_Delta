# Paper Trade Entry Autopsy

VNEDGE already has paper performance and exit autopsy reports. This report
fills the missing gap: it explains whether a closed paper trade opened from a
fresh, same-direction, fee-aware signal.

## What It Reads

- `logs/paper_trials/*.fills.jsonl`
- `logs/paper_trials/*.journal.jsonl`
- `lane_eval` records written by the live paper loop
- Closed paper trades reconstructed by the dashboard trade journal helpers

The report is read-only. It cannot trade, promote, demote, or modify a lane.

## What It Measures

For each paper lane, the report joins each closed trade's opening fill to the
most recent prior fired `lane_eval` from the same lane.

It then publishes:

- closed paper trades
- paper net PnL and average net bps
- average expected edge bps when the strategy journaled one
- average signal age in seconds
- average entry delay in bars
- stale-entry rate
- missing signal-context rate
- side drift rate
- recent entry contexts

## States

- `ENTRY_CAPTURE_HEALTHY`: entries are fresh, linked, same-direction, and net
  capture clears the configured quality gates.
- `ENTRY_CONTEXT_MISSING`: paper fills cannot be linked to fired `lane_eval`
  records. Fix journaling before judging the lane.
- `ENTRY_DIRECTION_DRIFT`: the entry side disagrees with the fired signal side.
  This is a wiring bug until proven otherwise.
- `ENTRY_SIGNAL_STALE`: entries are opening too long after the fired signal.
  Tighten signal TTL or reject late entries.
- `ENTRY_FEE_WALL_TOO_SMALL`: expected edge or realized net capture is too small
  for fees and slippage.
- `ENTRY_NEGATIVE_AFTER_COST`: entries are linked and fresh, but the lane still
  loses after fees.
- `ENTRY_UNDER_SAMPLED`: not enough closed paper trades to act.
- `ENTRY_NO_CLOSED_TRADES`: nothing closed yet.

## Why This Matters

Negative paper trading can come from two very different failures:

1. The scanner found a good setup, but the exit engine managed it badly.
2. The scanner entry itself was late, stale, side-flipped, or too small to pay
   the fee wall.

Exit autopsy handles the first case. Entry autopsy handles the second. The bot
should not promote any lane until both stories are clean.
