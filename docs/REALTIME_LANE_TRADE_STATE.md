# Realtime Lane Trade State

This layer answers the operator question: "is the lane actually trading, or is
it only close to a scanner trigger?"

`vnedge.research.realtime_scanner` now publishes these fields on each runtime
lane row:

- `final_why_no_trade`: the final runtime reason, preferring the runner heartbeat
  when a lane is already in a position.
- `latest_heartbeat`: compact runner state, quote freshness, counters, and the
  latest heartbeat reason.
- `trade_lifecycle`: the lane's current lifecycle stage and trade plan evidence.

## Lifecycle Stages

- `SCANNING`: live eval exists; thresholds are still below trigger.
- `NEAR_TRIGGER`: closest published gate is within the configured near-trigger
  ratio.
- `HIDDEN_VETO`: all published scanner gates passed, but no signal/order was
  emitted. Inspect cooldown, sizing, risk, route, position, and journal state.
- `SIGNAL_TO_ROUTE`: strategy fired on the latest eval; order/intent evidence is
  not yet observed.
- `SHADOW_INTENT_RECORDED`: shadow intent was emitted.
- `VIRTUAL_OUTCOME_RECORDED`: shadow outcome was resolved.
- `ENTRY_SUBMITTED`: paper order intent exists after the latest exit.
- `IN_POSITION`: runner heartbeat says the lane is managing an active trade plan.
- `EXIT_RECORDED`: latest observed paper lifecycle event is an exit.
- `STALE`: latest live eval is older than the lane timeframe freshness budget.

## Honest Exit Contract

The scanner exposes TP ladder evidence when the strategy emits it, and live
paper now preserves that ladder through account-store restart. It does not make
TP1/TP2/TP3 partial exits active. Current runtime behavior remains:

- stops: tick-stop when enabled, otherwise bar stop;
- take profit: the single `take_profit_price`;
- TP ladder: journaled progress only;
- BE-after-TP1, partial exits, and dynamic trailing ladder: not active yet.

That is intentional for this PR: it improves realtime truth without silently
changing trading behavior. The next execution PR can use this payload as the
acceptance contract for active TP1/BE/trailing capture.
