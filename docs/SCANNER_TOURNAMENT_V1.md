# Scanner Tournament V1

`scanner_tournament_v1` is the research-only answer to “make the scanners fire
more so we can find edge.” It deliberately lowers scanner discovery thresholds,
then ranks every Pine/Willy/Lux-inspired scanner group after realistic route
costs.

What is lowered:

- Research scanner edge floor, sample floor, and maker-fill floor.
- Edge-model sample floors for discovery ranking.
- The number of examples required before a scanner appears in the watchlist.

What is not lowered:

- `PreTradeRiskGateway.evaluate()`.
- Live mode, flag, and confirmation gates.
- Untouched-data judgment before promotion.
- Paper/live capital approval.

## Profiles

- `strict_proof`: current proof-grade thresholds. Best for “is this already
  close to promotable?”
- `paper_probe_candidate`: intermediate research triage. Best for designing a
  fresh untouched judgment or paper-probe request.
- `discovery_relaxed`: default service mode. Best for forcing scanners to emit
  enough opportunities for feature learning, fee-wall diagnosis, and AI ranking.

## Artifacts

The Docker service writes:

- `research/live_research/scanner_tournament_latest.json`
- `research/live_research/scanner_tournament_feed.jsonl`

Every artifact carries:

- `research_only=true`
- `can_trade=false`
- `can_promote=false`
- `requires_untouched_judgment=true`
- `lowered_governance_scope=research_discovery_only`
- `live_governance_unchanged=true`

## Reading Verdicts

- `STRICT_PROOF_WATCHLIST`: group already clears the strict 20 trade / 25 bps /
  PF 1.5 evidence screen on the research rows. Next action is a pre-registered
  untouched judgment window, not promotion.
- `DISCOVERY_WATCHLIST`: positive after costs under relaxed research settings.
  Keep collecting and train/rank with the edge model.
- `NEEDS_MORE_SAMPLES`: not enough routed examples yet.
- `WATCH_PF_WEAK`: average may be positive, but loss shape is still weak.
- `REJECT_NEGATIVE_AFTER_COST`: more firing did not create edge.
- `NO_ROUTED_TRADES`: thresholds, data, or scanner implementation still prevent
  executable opportunities.

This makes the bot more aggressive in research without becoming reckless in
capital.
