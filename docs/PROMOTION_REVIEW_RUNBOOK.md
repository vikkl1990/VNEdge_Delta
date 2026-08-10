# Promotion Review Runbook

`promotion_review_runbook_v1` is the operator packet between a passed
walk-forward candidate and any human paper-trial review.

Inputs:

- `research/live_research/feed.jsonl`
- `research/judgments/burn_registry.jsonl`
- `research/paper_trials/`
- the code-calculated `promotion_red_team_v1` charges

Outputs:

- `research/live_research/promotion_red_team_latest.json`
- `research/live_research/promotion_review_runbook_latest.json`
- `research/live_research/promotion_review_runbook_feed.jsonl`

The runbook does not trade, promote, edit manifests, change thresholds, or open
live gates. It only classifies passed candidates as:

- `BLOCKED_BY_RED_TEAM`
- `NEEDS_OPERATOR_ANSWERS`
- `HUMAN_REVIEW_READY`

Every row carries `can_trade=false` and `can_promote=false`. A
`HUMAN_REVIEW_READY` row means only that the candidate has a defensible packet
for a human paper-trial review; it is not live approval.
