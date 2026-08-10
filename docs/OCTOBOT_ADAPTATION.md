# OctoBot Adaptation Notes

Reviewed source: `Drakkar-Software/OctoBot`.

## Useful Ideas

OctoBot's most useful pattern for VNEDGE is the separation between:

- hard optimizer filters: reject runs that do not clear minimum evidence
- weighted fitness parameters: rank surviving or near-miss runs by multiple
  metrics rather than one headline PnL number

VNEDGE already has stricter promotion gates, so this is not a replacement for
walk-forward, untouched-data judgment, paper/shadow proof, or live safety gates.
It is an explanatory operator surface.

## Adapted

`vnedge.research.optimizer_scorecard` adds an OctoBot-inspired research
scorecard for edge leaderboard rows. Each row now exposes:

- hard filters: minimum OOS trades, positive net after fees, minimum PF
- weighted components: PF, sample size, net after fees, payoff, fee multiple,
  and profitable-window consistency
- near-miss flag: high weighted fitness with one or more failed hard filters

The edge leaderboard folds this under `optimizer_fitness`, and promotion queue
entries carry the same object for UI/API consumers.

## Safety Boundary

The scorecard is read-only:

- `can_trade=false`
- `can_promote=false`
- no route decision changes
- no promotion tier changes
- no live-path imports

It answers: "why is this lane blocked or close?" It never answers: "may this
trade live?"

## Not Adopted

VNEDGE should not adopt OctoBot's general plugin/tentacle architecture, generic
automation DSL, community/copy-trading surfaces, or broad live bot model for the
current v1. VNEDGE's execution path remains deliberately narrower: gateway,
journal, reconciliation, mode ladder, and explicit human promotion.
