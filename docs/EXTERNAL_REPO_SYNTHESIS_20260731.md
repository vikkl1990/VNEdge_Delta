# External Repo Review Synthesis

Date: 2026-07-31

This document consolidates the public repository reviews performed for VNEDGE
into one implementation queue. These repos are references for architecture,
operator experience, research workflow, and account visibility. They are not
sources of tradable proof, and VNEDGE does not copy protected strategy code.

## Sources Reviewed

| Source | VNEDGE Signal | Adopt | Do Not Adopt |
|---|---|---|---|
| `Hitheshkaranth/OpenTerminalUI` | Production terminal workspace | Fixed rail, GO command, market tape, dense shell | Browser order controls |
| `OpenBB-finance/OpenBB` | Data/provider platform | Provider lineage, research data fabric, AI-friendly APIs | Unvetted external feeds on live execution |
| `Fincept-Corporation/FinceptTerminal` | Professional terminal UX | Multi-pane analytics hierarchy, terminal navigation | Generic equity-terminal scope creep |
| `microsoft/qlib` | Quant research lifecycle | Experiment registry, model freshness, feature governance | Model score as promotion permission |
| `coding-kitties/investing-algorithm-framework` | Backtest workflow and evidence organization | Factor ranking, searchable evidence, parameter bundle archival | Generic live deployment wrapper |
| `evan-kolberg/prediction-market-backtesting` | Event settlement discipline | Pre-declared outcomes, probability calibration, settlement ledger | Binary event scoring replacing exchange fills |
| `AI4Finance-Foundation/FinRobot` | Financial agent roles | Analyst/risk/research agents, artifact explanations | LLM-issued orders |
| `chrisworsey55/atlas-gic` | Agent loop with scored outcomes | Keep/decay/retire loop, regime memories | Prompt mutation against burned data |
| `The-Swarm-Corporation/AutoHedge` | Specialist swarm research | Adversarial risk review, hedge suggestions as tasks | Minimal-human-intervention live trading |
| `OpenHands/OpenHands` | Durable agent task control plane | Task/event/artifact ledger, progress visibility | Agent access to secrets |
| `cobusgreyling/loop-engineering` | Loop-engineering discipline | Maker/verifier split, loop readiness, memory | Unbounded autonomous loops |
| `JerBouma/FinanceToolkit` | Transparent financial metrics | Metric definitions, account diagnostics | Equity fundamentals as scalp alpha |
| `ghostfolio/ghostfolio` | Account/portfolio management | Net-worth timeline, allocations, fee/funding ledger | Account actions bypassing order manager |

## Consolidated Build Tracks

1. `terminal_operator_shell_v1`

   Build a trader-grade shell around real VNEDGE state: workspace rail, GO
   command routing, market tape, live-lock/gateway/build provenance, and dense
   panels. This is the UI pass in the consolidated PR.

2. `agentic_research_os_v2`

   Extend Quant OS so agents become durable research workers: task ledger,
   event stream, artifact registry, verifier state, hypothesis memory, and
   measured keep/decay/retire status. Agents remain research-only.

3. `experiment_lineage_factory_v1`

   Normalize every Pine, Quantified, scanner, replay, paper, and prediction-style
   result into a single evidence table keyed by source hash, data window, cost
   model, verdict, and fee-wall outcome. Negative results stay useful.

4. `account_control_center_v1`

   Build a Ghostfolio/FinanceToolkit-style capital view for VNEDGE: account
   equity, exposure, leverage, margin, lot rounding, fees, funding, realized
   PnL, unrealized PnL, drawdown, and per-profile risk.

5. `provider_fabric_v1`

   Add research-only provider lineage for external context such as macro,
   calendar, realized volatility, funding, and open-interest features. Provider
   data can rank or label research, but cannot directly trigger live orders.

## Safety Boundary

The synthesis is exposed by:

- `vnedge.research.external_repo_synthesis`
- dashboard route `/external-repo-synthesis`
- the cockpit's System view panel

It is explicitly:

- `research_only=true`
- `can_trade=false`
- `can_promote=false`

It does not relax gates, alter strategies, import external runtime code, or
create any live execution path.

## Recommended Next PR Order

1. Merge the consolidated synthesis/terminal shell PR.
2. Build `codex/agentic-research-os-v2`: scoring, decay, verifier, artifact
   states in the Quant OS task ledger.
3. Build `codex/experiment-lineage-factory`: one evidence table across Pine,
   Quantified, scanner, replay, paper, and prediction-style settlement.
4. Build `codex/account-control-center`: profile-level margin/leverage input,
   fee/funding ledger, and closed-trade drift truth.
5. Build `codex/research-provider-fabric`: OpenBB/FinanceToolkit-style provider
   wrappers for research-only context enrichment.
