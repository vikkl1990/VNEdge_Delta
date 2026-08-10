# Pine Intention Distiller - 2026-07-19

The Pine Research Lab was too inventory-heavy.  It showed source availability,
port queues, and backtest cells, but it did not answer the operator's real
question: what is each indicator trying to trade, which parts are usable for a
crypto bot, and what exact replay should prove or kill it?

This change adds a source-backed intention layer to `pine_alpha_distiller_v1`.
The distiller still never emits full Pine source into runtime artifacts or the
dashboard.  It reads local source artifacts, hashes them, extracts causal
trading atoms, and publishes a per-script trading brief:

- trading thesis
- context layer
- setup
- trigger
- execution bias
- exit plan
- bot use
- backtest recipe
- portable atoms
- non-portable atoms
- agent uplift questions

## Current Corpus Read

Local source-backed corpus in this checkout:

- 427 scripts reviewed from `research/pine_scripts/sources`
- 307 source-backed port candidates
- 117 causality/HTF repaint quarantine rows
- 3 feature-bank/library-only rows
- 3,684 queued venue/timeframe proof cells

Ranked intention clusters after removing old metadata echo from primitive
detection:

| Cluster | Sources | Port candidates | VNEDGE route |
| --- | ---: | ---: | --- |
| liquidity_sweep_reclaim | 56 | 56 | `fvg_liquidity_breakout_v1` |
| liquidity_zone_breakout | 56 | 56 | `fvg_liquidity_breakout_v1` |
| range_expansion_breakout | 45 | 45 | `range_expansion_breakout_v1` |
| trend_momentum_filter | 38 | 38 | `trend_momentum_context_v1` |
| general_feature_bank | 33 | 33 | `edge_model_feature_bank_v1` |
| orderflow_absorption | 32 | 32 | `orderflow_proxy_v1` |
| adaptive_trail_exit | 26 | 26 | `trail_exit_lab_v1` |
| momentum_feature_bank | 23 | 21 | `edge_model_feature_bank_v1` |
| causality_quarantine | 117 | 0 | `causality_quarantine_v1` |

The highest-ranked executable build remains `fvg_liquidity_breakout_v1`, now
split into two clear playbooks:

- liquidity sweep/reclaim: 1h bias, 15m liquidity map, 5m reclaim or
  displacement trigger, structural stop, room-to-liquidity, TP ladder.
- liquidity zone breakout: FVG/order-block/support-resistance zone, acceptance
  or rejection away from the zone, volume confirmation, maker-first execution
  where possible.

## Executable Port Status

`fvg_liquidity_breakout_v1` is now a VNEDGE-owned strategy implementation, not
just a queue label. It runs as a causal 5m trigger / 15m setup / 1h bias scanner
with:

- active FVG zone state and one-bar-minimum retest age
- sweep/reclaim, structure-break, displacement, and volume-z gates
- room-to-liquidity and expected-net-bps checks against taker fallback costs
- structural SL plus TP1/TP2/TP3 metadata, `BE_after_TP1`, and
  `smart_capture=TP1_or_trail`

The next step is evidence, not promotion: run the scanner through fee-wall
forensics and the Pine backtest publisher across venues/timeframes, then judge
only fresh windows that survive sample/PF/net-bps gates.

## Why This Matters

The bot should not blindly copy Pine chart code.  Most public indicators mix
three things:

- useful trading intention
- visual/UI state
- repaint-prone or non-executable convenience logic

VNEDGE should copy none of that directly into live runtime.  It should distill
the intention, port only causal atoms into VNEDGE-owned Python, then replay with
fees, slippage, maker/taker routing, stop-first exits, and untouched-window
judgment.

The new dashboard layer makes that visible per script and per playbook, so the
896-script discovery effort turns into a ranked alpha factory queue instead of
a raw catalog.
