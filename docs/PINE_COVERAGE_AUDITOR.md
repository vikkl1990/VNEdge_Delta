# Pine Coverage Auditor

Status: research-only. This artifact never grants trade or promotion rights.

The Pine Research Lab now publishes `coverage_audit` inside
`research/pine_scripts/pine_research_kb.json` and serves it from
`/pine-research/kb`.

## Purpose

The broad TradingView discovery run can be larger than the active
source-backed KB. That is expected: catalog metadata, protected scripts,
browser failures, and duplicate source exports are not executable strategy
evidence.

The coverage auditor keeps those buckets visible so the operator can see:

- discovered records vs active KB rows,
- source-backed rows,
- catalog backlog not loaded into the active KB,
- retryable browser extraction errors,
- source-tab failures,
- deduped source exports,
- source-backed causal port queue,
- repaint/HTF causality quarantine,
- completed-but-negative replay evidence.

## CLI

When a separate crawler knows the original catalog universe size, preserve it:

```bash
python -m vnedge.research.pine_script_research \
  --source-dir research/pine_scripts/sources \
  --extraction-manifest research/pine_scripts/extraction_manifest.jsonl \
  --discovery-total 908 \
  --output research/pine_scripts/pine_research_kb.json \
  --no-defaults
```

If `--discovery-total` is omitted, the auditor still reports exact active-KB
and extraction-manifest counts, but it cannot invent missing catalog rows that
were never persisted.

## Safety Rule

Catalog-only rows and extraction failures are prioritization data only. A row
becomes actionable only after:

source-backed artifact -> causality rewrite/review -> VNEDGE-owned Python port
-> fee-aware replay -> untouched-window judgment -> shadow/paper promotion.
