# LLM narration policy — numbers are code-calculated, LLMs only narrate

**Status: binding policy.** Adapted from FinRobot's separation of duties. This
codifies the `can_trade = False` stance for any use of a language model in
VNEDGE, present or future.

## The rule

1. **Every number is computed by code.** PnL, profit factor, drawdown, Sharpe,
   position size, gate verdicts, promotion decisions — all are produced by the
   deterministic code paths (backtester, metrics, gateway, walk-forward gates,
   `promotion_red_team`). An LLM never computes, estimates, or "adjusts" a
   number that reaches a decision.

2. **LLMs only narrate already-journaled results.** An LLM may turn
   *already-computed, already-journaled* facts into prose — a rejection
   explanation, a lane summary, a research note. It reads the record; it never
   creates the record. If a sentence contains a figure, that figure was computed
   by code and can be traced to a journal/ledger entry.

3. **LLMs never touch the hot path.** No model call sits in the order path, the
   `PreTradeRiskGateway`, position sizing, reconciliation, or the promotion
   decision. LLM work is **offline**, over data that already exists.

4. **Everything is provenance-tracked.** Narration cites its source (a run id, a
   journal line, a `data_burn` window). An unprovenanced claim is a bug.

5. **Adversarial use is code, not model.** The anti-promotion red-team
   (`research/promotion_red_team.py`) argues the bear case with **code-calculated
   charges**. An LLM may later *narrate* those charges; it may never *invent*
   one. The judge's verdict is deterministic.

## Why

The generation leverage of an LLM (writing a strategy, explaining a rejection)
is worth having; its tendency to fabricate plausible numbers is not. Drawing the
line at "narrate, never calculate" keeps the leverage and firewalls the risk —
the same discipline the AI strategy sandbox enforces structurally
(`strategy/ai_sandbox.py`, `strategy/plugin_manifest.py`: `can_trade = False`).

## Enforcement

- No LLM dependency exists in the trading/execution/sizing/gateway code paths,
  and none may be added there.
- Any future LLM surface must consume already-journaled data and emit text only;
  its output is never parsed back into a numeric decision.
- The RD-Agent-style auto-generate → auto-backtest → auto-promote loop is
  explicitly **out of scope**: it is a multiple-comparisons machine that attacks
  the anti-overfit discipline. Any such loop must sit behind pre-registered
  untouched-data judgment (`data_burn`), never auto-promoting.
