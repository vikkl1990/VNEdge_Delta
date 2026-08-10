# VNEDGE incident runbooks

One short section per incident type. Each incident on the dashboard's
Incidents panel links to its anchor here. Everything on this page is
read-only guidance — the dashboard itself has no control routes, so every
action below happens on the host running the bot.

## General triage

Symptoms: an incident type without a dedicated section, or several
incidents at once.

First checks:
- Open the dashboard operator board: risk status, feed age, journal status.
- Tail the lane journal: `tail -50 logs/paper_trials/<lane_id>.journal.jsonl`.
- Tail the alerts file the incident came from (`*.alerts.jsonl` /
  `logs/alerts.jsonl`).

Safe actions:
- Reading files and the dashboard is always safe.
- When in doubt, trip the kill switch (`touch KILL` in the bot's cwd):
  it blocks new entries but never blocks reduce-only exits.
- Do not delete or edit journals, ledgers, or account stores — they are the
  recovery baseline.

## Kill switch and flatten

Symptoms: `kill_switch` alert, `emergency_flatten_started` /
`emergency_flatten_finished` journal records, dashboard chip shows
"kill active", entries blocked.

First checks:
- Why did it trip? The kill switch reason is in the alert message and in the
  journal around the trip time.
- Is a flatten in progress? `emergency_flatten_started` without a matching
  `emergency_flatten_finished` means exits are still working.

Safe actions:
- The kill switch NEVER auto-resets. Clearing it is a deliberate human act:
  remove the `KILL` file and restart the session only after you understand
  and have fixed the cause.
- Exits remain allowed while it is active — do not fight the flatten.

## Reconciliation fail closed

Symptoms: `reconciliation_fail_closed` journal record; risk status
`reconciling`; new entries blocked, lane reduce-only.

First checks:
- Read the mismatch list in the journal record payload (local vs venue
  positions/orders).
- Check for `order_timeout_unknown` records just before it — an unresolved
  order is the usual cause.

Safe actions:
- Do nothing that creates new risk. The lane rebuilds state from the
  exchange and resumes only after a clean reconciliation pass.
- If the mismatch persists across restarts, keep the lane reduce-only and
  compare the account store with venue state before touching anything.

## Orphaned paper position

Symptoms: `orphaned_paper_position` journal record — a restored position
exists but its exit plan could not be restored with it.

First checks:
- Journal payload shows the symbol/side/quantity that is unprotected.
- Confirm on the dashboard Positions panel that the position is real.

Safe actions:
- The session manages the orphan reduce-only. If unsure, trip the kill
  switch — exits stay available and the position can still be closed.
- Never delete the account store to "fix" it; that discards the position
  record, not the position.

## Plan restore rejected

Symptoms: `plan_restore_rejected` journal record on resume — a persisted
exit plan failed validation and was not restored.

First checks:
- Journal payload carries the rejection reason (wrong symbol, absurd
  levels, malformed store).
- Check whether an open position now lacks a stop/target plan (see
  orphaned paper position above).

Safe actions:
- Do not hand-edit the account store to force a restore.
- If a position is open without a plan, treat it as an orphan: reduce-only,
  kill switch if in doubt.

## Feed stale

Symptoms: `feed_stale` alert; feed age above 120s; freshness gate blocks
entries.

First checks:
- Is it one venue or all? Compare lanes on the Connections panel.
- Host network / venue status page / websocket reconnect messages in the
  process log.

Safe actions:
- The freshness gate already blocks new entries on stale data — no action
  needed to make it safe.
- Restarting the session is safe: state restores from the account store and
  journals.

## Journal unavailable

Symptoms: `journal_unhealthy` alert; `last_journal_write` not "ok"; new
risk-increasing orders rejected, exits only.

First checks:
- Disk full? Permissions? `df -h` and try `touch` next to the journal file.
- Process log has the exact OSError that flipped it.

Safe actions:
- Fix the storage problem, then restart — the journal probe runs at
  startup. The exits-only stance until then is correct: if we cannot record
  decisions, we do not create new risk.

## Risk status degraded

Symptoms: `risk_status` alert with a non-ok status such as `reconciling`
or `lane_error`.

First checks:
- Which lane and which status? The alert message carries it; the lane
  journal has the trigger.
- Look for unresolved orders (timeout_unknown) on the Execution Truth panel.

Safe actions:
- Entries are already restricted while degraded. Let reconciliation resolve
  unknown orders; investigate rather than restart-loop the process.

## Daily loss stop

Symptoms: `daily_loss` alert; daily PnL at or below the configured limit;
entries halted for the day.

First checks:
- Confirm the number against the equity curve and fills — one bad trade or
  many small ones?
- Check fee drag: fees count against the day.

Safe actions:
- The halt is the safety feature working. Do NOT raise the limit to keep
  trading — risk configs are frozen; limit changes require a restart and
  are a deliberate decision, never a mid-drawdown reaction.

## Loss streak

Symptoms: `loss_streak` alert; 3+ consecutive losing round trips; the
consecutive-loss breaker may halt entries.

First checks:
- Are the losses one strategy/lane or spread out?
- Regime check: has the market state moved against the strategy's regime
  filter?

Safe actions:
- Let the breaker do its job. Review the trades in the journal before
  resuming; do not tune parameters in response to a streak (that is the
  overfitting trap).

## Drawdown

Symptoms: `drawdown` alert; equity below peak by more than the trial
envelope.

First checks:
- Dashboard drawdown KPI and equity curve: fast crash or slow bleed?
- Compare against the trial's locked max-drawdown criterion — breaching it
  fails the trial; that is a verdict, not a tuning signal.

Safe actions:
- If the envelope is breached, stop the trial and record the result. Do not
  restart with looser limits.
- Kill switch (`touch KILL`) is always a safe way to stop new entries while
  you review; it never auto-resets.
