# Pine Script Research Lab

Status: read-only research surface.

Purpose: build a durable knowledge base for public/open-source TradingView
scripts and user-supplied Pine exports, then decide which ideas deserve a
causal VNEDGE port and multi-timeframe crypto replay.

## Why this exists

TradingView has a large public script catalog and an open-source discovery
mode, but catalog pages do not reliably expose Pine source in static HTML, and
many scripts are protected or invite-only. VNEDGE therefore treats each script
as a provenance-bound research record:

1. discover metadata and links,
2. ingest source only when it is public/open-source or supplied by the user,
3. hash the source and record license/provenance,
4. run AI review for crypto portability and repaint/lookahead risk,
5. port only clean mechanisms into VNEDGE strategy classes,
6. replay on all configured timeframes/venues with fees and slippage,
7. require untouched-window judgment before shadow/paper promotion.

No script review can trade or promote directly.

## Dashboard surface

New route:

- `/pine-research` serves the standalone Pine Research Lab page.
- `/pine-research/kb` serves the token-gated JSON knowledge base.
- `/pine-research/distiller` serves the token-gated source-backed primitive
  and VNEDGE port-task queue.
- `/pine-research/rule-spec` serves the token-gated local safe-subset compiler
  and Delta-candle evaluation status. It never calls TradingView or the
  unofficial `tvscreener` endpoints.
- `/pine-research/scanner-uplift` serves the token-gated fee-wall/near-miss
  uplift report from completed scanner backtests.
- `/pine-research/quant-loop-governance` serves the token-gated research-loop
  readiness, collision, budget, and gate status.
- `/pine-research/evidence-index` serves the token-gated unified evidence
  table across Pine Lab, scanner tournament, scanner uplift, Alpha Arena,
  fee-wall, contract-matrix, and replay artifacts.

The endpoint falls back to a seed payload when the generated artifact is absent,
so the page remains useful during early deployment. The expected generated
artifact is:

```text
research/pine_scripts/pine_research_kb.json
```

`create_app(..., pine_research_path=...)` can point the dashboard at another
artifact path.

The source-backed distiller publishes:

```text
research/live_research/pine_alpha_distiller_latest.json
```

In production Compose this is refreshed by the `pine-alpha-distiller` service
at startup and on `PINE_ALPHA_DISTILLER_INTERVAL_SECONDS` cadence. This keeps
the distiller JSON in sync with UI/code deploys instead of relying on a manual
one-shot artifact publish.

The backtest matrix is refreshed by a third artifact step:

```bash
python -m vnedge.research.pine_backtest_evidence \
  --kb research/pine_scripts/pine_research_kb.json \
  --distiller research/live_research/pine_alpha_distiller_latest.json \
  --report-dir research/live_research
```

This command overlays VNEDGE-owned primitive evidence from artifacts such as
`alpha_distillation_latest.json`, `daily_scalper_cadence_latest.json`,
`orderflow_footprint_latest.json`, `candidate_replay_latest.json`, and
`event_leadlag_latest.json` onto each source-backed Pine row. It also consumes
`fee_wall_forensics_latest.json`, which is the broadest current fee-aware
replay surface for Luxara/Luxy/Stealth/SATS-style causal Python scanners. A
completed cell means "the matching VNEDGE primitive has evidence"; it does
**not** mean the original Pine script was executed, copied, or approved for
shadow/paper/live.
Rows still require a causal Python port and untouched-window judgment before
any promotion discussion.

For the supplied `VNEDGE ALGO ML Pro` Pine scanner, the Pine Lab now has a
dedicated exact-lifecycle Delta contract-risk matrix:

```bash
python -m vnedge.research.vnedge_algo_ml_pro_contract_matrix \
  --data-root data \
  --exchange delta_india \
  --symbols BTCUSD ETHUSD SOLUSD XRPUSD BNBUSD DOGEUSD \
  --timeframes 1m 5m 15m 1h 4h \
  --capture-modes pine_tp3 smart_ladder \
  --sizing-mode delta_contract_risk \
  --delta-live-product-spec \
  --account-equity-usd 500 \
  --risk-per-trade-pct 1 \
  --paper-margin-usd 100 \
  --paper-leverage 25 \
  --acknowledge-high-leverage \
  --fee-cost-bps 12.5
```

The scanner backtest uplift layer then reads that matrix plus the broad scanner
tournament and classifies every row as promotable, sparse-positive, fee-wall
near-miss, visual-only, overscalp bleed, or reject:

```bash
python -m vnedge.research.scanner_backtest_uplift \
  --input research/live_research/vnedge_algo_ml_pro_contract_matrix_latest.json \
  --source-name vnedge_algo_ml_pro_contract_matrix \
  --input research/live_research/scanner_tournament_latest.json \
  --source-name scanner_tournament \
  --out research/live_research/scanner_backtest_uplift_latest.json
```

Production Compose refreshes both artifacts through
`vnedge-algo-ml-pro-contract-matrix` and `scanner-backtest-uplift`, so the
operator should no longer see a static "queued" answer after the backtest has
actually completed.

`alpha-arena-lite` consumes scanner uplift, scanner tournament, and fee-wall
artifacts, then publishes `research/live_research/alpha_arena_lite_latest.json`.
It converts sparse or near-fee-wall positives into durable Quant OS tasks and
canonical strict fee-wall breakers into untouched-judgment tasks, without
granting paper/live permission.

`quant-loop-governance` then watches the Pine/Scanner/Arena loop:

```bash
python -m vnedge.research.quant_loop_governance \
  --gates governance/loop_gates.yaml \
  --state research/quant_loop_state.json \
  --alpha-arena research/live_research/alpha_arena_lite_latest.json \
  --scanner-uplift research/live_research/scanner_backtest_uplift_latest.json \
  --scanner-progress research/live_research/scanner_tournament_progress.json \
  --gateway-snapshot logs/agent_gateway/quant_os/snapshot.json
```

Its output is a control-plane artifact only. It can mark the research loop
healthy, stale, over-budget, or blocked by duplicate candidate locks, but it
cannot start paper/shadow/live lanes.

`evidence-index-publisher` reconciles the research artifacts into one operator
truth table every few minutes:

```bash
python -m vnedge.research.evidence_store \
  --interval-seconds 300 \
  --report-dir research/live_research \
  --pine-kb research/pine_scripts/pine_research_kb.json \
  --out research/live_research/evidence_index_latest.json \
  --sqlite research/live_research/evidence_index.sqlite \
  --feed research/live_research/evidence_index_feed.jsonl
```

The index is still research-only. A strict fee-wall breaker in the index is a
candidate for causal-port/replay/untouched-window judgment, not paper/live
permission.

The production Compose dashboard mounts this directory read-only:

```text
./research/pine_scripts:/app/research/pine_scripts:ro
```

Publish or refresh the artifact with:

```bash
python -m vnedge.research.pine_script_research \
  --source-dir research/pine_scripts/sources \
  --output research/pine_scripts/pine_research_kb.json
```

Explicit user-supplied exports can be reviewed without scanning the directory:

```bash
python -m vnedge.research.pine_script_research \
  ~/Downloads/open_indicator.pine \
  --source-dir /path/that/does/not/exist \
  --output research/pine_scripts/pine_research_kb.json
```

Use `--no-defaults` for a clean artifact with only supplied/open-source files.

Bulk TradingView catalog links can be added as metadata-only backlog:

```bash
python -m vnedge.research.pine_script_research \
  --catalog-url https://www.tradingview.com/scripts/ \
  --source-dir research/pine_scripts/sources \
  --output research/pine_scripts/pine_research_kb.json \
  --max-catalog-records 250
```

For the broad TradingView intake, use the built-in accessible discovery preset.
It fetches public catalog/tag pages such as scripts, indicators, strategies,
crypto, BTC/ETH, scalping, day trading, SMC/liquidity, order blocks, FVG, VWAP,
RSI, MACD, Supertrend, and Bollinger pages, then optionally follows one hop of
additional `/scripts/.../` category links:

```bash
python -m vnedge.research.pine_script_research \
  --include-tradingview-discovery \
  --discovery-depth 1 \
  --max-discovery-pages 80 \
  --source-dir research/pine_scripts/sources \
  --output research/pine_scripts/pine_research_kb.json \
  --max-catalog-records 1000
```

This imports every public script listing discovered inside the configured page
and record caps. It still stores these as `CATALOG_METADATA_ONLY` unless a
lawful Pine source export is present.

For large profile/tag pages, save the HTML once and import it in chunks:

```bash
python -m vnedge.research.pine_script_research \
  --catalog-html research/pine_scripts/catalog/tradingview_scripts_page_1.html \
  --source-dir research/pine_scripts/sources \
  --output research/pine_scripts/pine_research_kb.json \
  --no-defaults
```

Catalog rows are intentionally `BLOCKED_NO_SOURCE`. They are useful for
prioritization and AI clustering, but cannot be ported or backtested until
lawful public/open-source Pine source is supplied.

The dashboard distinguishes source states explicitly:

- `SOURCE_BACKED`: VNEDGE has a `.pine`, `.pinescript`, or `.txt` source
  artifact with line count and source hash.
- `SOURCE_BACKED_CATALOG_MATCH`: a source-backed artifact was reconciled to a
  catalog URL, so the source wins and the listing is retained as provenance.
- `CATALOG_METADATA_ONLY`: VNEDGE found a TradingView listing, but the
  executable Pine is not present in the catalog artifact. This is not
  backtestable yet.
- `SOURCE_MISSING`: an idea record exists without a source artifact or catalog
  URL.

To turn a `CATALOG_METADATA_ONLY` row into a port candidate, confirm the author
has exposed source and place the exported/pasted Pine file under:

```text
research/pine_scripts/sources/
```

When the source came from an open-source TradingView page, also keep a local
JSONL extraction manifest. The manifest is provenance only: it records URL,
status, output file, source line count, and hash. The `.pine` files and manifest
are runtime artifacts and remain gitignored; the published KB stores only the
review metadata and hash evidence.

```bash
python -m vnedge.research.pine_script_research \
  --source-dir research/pine_scripts/sources \
  --extraction-manifest research/pine_scripts/extraction_manifest.jsonl \
  --include-tradingview-discovery \
  --discovery-depth 1 \
  --max-discovery-pages 80 \
  --max-catalog-records 1000 \
  --output research/pine_scripts/pine_research_kb.json \
  --no-defaults
```

The browser extractor must only use the visible `Source code` tab on pages that
TradingView marks as open-source. Protected/invite-only scripts stay
`CATALOG_METADATA_ONLY` or a blocked manifest row; screenshots/descriptions are
never treated as executable source.

When a catalog URL and a source-backed Pine export look like the same script,
the publisher reconciles them into one source-backed record. The source record
wins, while `catalog_urls` and `catalog_script_ids` preserve discovery
provenance. This prevents the lab from double-counting a script after source is
supplied.

## Knowledge-base shape

Each record contains:

- `script_id`, title, URL, author, kind,
- source availability, license, line count, SHA-256 hash,
- source status, explanation, and next source step,
- detected features and risks,
- crypto portability verdict,
- crypto fit score,
- porting notes,
- AI uplift ideas,
- mechanism cluster, priority score, and next action,
- timeframe backtest cells,
- decision and promotion flags.

Promotion flags are always false at this layer.

The artifact also contains `coverage_audit` (`pine_coverage_auditor_v1`), which
explains the full discovery funnel: discovered records, visible KB rows,
source-backed rows, catalog backlog, browser retry errors, source-tab failures,
port queue, causality quarantine, and completed replay cells. Use
`--discovery-total` when a bulk crawler has a larger catalog count than the
active source-backed KB.

`next_action` is the operator queue:

- `REQUEST_OPEN_SOURCE_EXPORT`: catalog hit only; source is still required.
- `PORT_CAUSAL_FEATURES_AND_REPLAY`: source exists and the mechanism can be
  ported into VNEDGE for causality tests and replay.
- `RUN_CAUSALITY_AUDIT`: source exists but repaint/lookahead risk is present.
- `DISTILL_FEATURES_ONLY`: useful as ML/features, not a standalone scanner.

## Verdict meanings

- `PORTABLE`: clean enough to port, still needs VNEDGE replay.
- `PORTABLE_WITH_CHANGES`: useful mechanism, but requires causal/execution fixes.
- `RESEARCH_ONLY`: useful as a feature or teaching overlay, not a standalone lane.
- `BLOCKED_NO_SOURCE`: metadata only; no lawful/causal port can be built yet.
- `BLOCKED_REPAINT_RISK`: MTF/lookahead/repaint risk must be resolved first.

## Current seed records

The initial page includes:

- TradingView public scripts catalog: discovery record, source blocked.
- Luxara Live Plan QTM: ported as `luxara_live_plan_qtm_v1`; research candidate
  only after same-data VM replay.
- Luxara Break & Bounce V27: ported as `luxara_break_bounce_v27_v1`; telemetry
  only because broad breakouts failed and the strict pulse is sparse.

## Chunked research loop

The production-grade loop publishes `research/pine_scripts/pine_research_kb.json`
in chunks:

- catalog/import batch,
- source-hash batch,
- AI review batch,
- port/backtest queue batch,
- evidence overlay batch (`pine_backtest_evidence_v1`),
- dedupe against existing VNEDGE scanner families.

The crawler must not scrape or store protected/invite-only source. It should
only review metadata for those and mark them `BLOCKED_NO_SOURCE`.
