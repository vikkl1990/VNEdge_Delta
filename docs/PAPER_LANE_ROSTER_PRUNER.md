# Paper Lane Roster Pruner

The paper lane governor is the read-only roster pruner for scanner evaluation.
It does not trade, promote, demote, or edit manifests. It decides what the next
human action should be for each paper lane based on lane survival evidence.

## Active Paper Roster Rule

Only cost-positive or flat under-sampled lanes can stay in the active paper
roster. A lane with negative closed paper PnL is no longer kept active merely
because its sample is small.

Negative paper lanes now split into two explicit queues:

- `DEMOTION_QUEUE`: enough negative evidence exists to move the lane back to
  shadow/research before it consumes more paper cycles.
- `PROBATION_QUEUE`: the lane is negative but under-sampled; hold it out of the
  active paper roster and mine entry/exit failures before adding more exposure.

## Why This Matters

The bot was previously evaluating too many lanes at once, and some
negative-under-sampled lanes still appeared as paper-roster candidates. That
made the UI look busy while the real edge picture stayed muddy. The pruner keeps
the active paper roster reserved for lanes that are not currently bleeding after
fees.

## Promotion Discipline

This does not lower governance. Promotion still requires:

- fresh route/cadence/journal proof,
- clean ledger pairing,
- at least 20 closed paper trades,
- PF >= 1.5,
- average net edge >= 25 bps,
- human review before any live step.

