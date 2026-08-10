# VNEdge_Delta — Local Delta India Research Laboratory

VNEdge_Delta is a local, causal, research-only laboratory for Delta Exchange
India market data, focused on `BTCUSD` and `ETHUSD`.

> **Current truth:** there is no validated after-cost trading edge. Paper and
> live trading are locked, every retired primary candle scanner is disabled,
> and the Delta research runtimes construct no broker or order route.

This repository is not a live trading bot and it is not financial advice.
Crypto derivatives can lose the entire amount committed to them.

## Safety state

The Delta research boundary is fail-closed:

```text
research_only = true
can_trade     = false
can_promote   = false
validated_edge = false
order_route   = absent
broker        = absent
```

The repository contains generic paper, risk, and execution scaffolding from an
older multi-venue architecture. Those modules are supporting or historical
code; they are not connected to the Delta research sidecar. Their presence is
not evidence that this project is paper-ready or live-ready.

## What is active

- Closed-candle causal research on Delta BTCUSD and ETHUSD.
- Immutable multi-timeframe context from proven-closed candles.
- Realistic Delta fee, GST, slippage, and next-bar outcome accounting.
- Append-only research journaling, chronological validation, and sealed tails.
- Public trade and L2 event recording with exchange and local timestamps.
- Sequence/checksum gap detection and deterministic event replay tooling.
- Read-only local dashboard with Delta research identity as the homepage.

The active research direction is **event-time data integrity and deterministic
replay**. Live event capture is feature/telemetry-only. No absorption,
order-flow, queue, or forced-flow hypothesis is eligible for paper or live use
until replay fidelity and after-cost edge are independently proven.

## What is not working

The engineering laboratory works; the tested alpha does not. The following
primary hypotheses are retired as rejected research benchmarks:

- Momentum Burst
- Imbalance Fade
- Hierarchical Pullback
- BTC → ETH Lead-Lag
- Range Compression Breakout
- Session Liquidity Sweep
- Continuous Multi-Timeframe Alignment v1/v2

They must not be silently re-enabled in the continuously running Delta
sidecar. Frozen historical contracts remain loadable only so old results can
be reproduced.

## Research correctness protocol

Every new hypothesis must follow this order:

1. Freeze a written preregistration before looking at results.
2. Test gross edge on the selection window.
3. Apply realistic Delta fees, GST, and slippage.
4. Validate chronologically.
5. Open one sealed untouched window only after earlier gates pass.
6. Require positive after-cost expectancy, adequate profit factor, sample
   size, data quality, and market consistency.

Forbidden shortcuts include tuning after the first selection look, applying a
meta-model to rescue a standalone losing scanner, lowering costs to force
trades, or treating trade frequency as a target.

## Time and data guarantees

- A decision uses only information available at its decision timestamp.
- Candle decisions occur after a proven close; default fills begin at the next
  bar unless a preregistered contract states otherwise.
- Higher-timeframe state must be available before lower-timeframe evaluation.
- Funding is usable only after its settlement/publication timestamp.
- Missing intervals and broken L2 sequences fail closed.
- Live and replay share the same feature and decision components.
- Performance is reported after costs unless explicitly labelled gross.

## Local dashboard

Start the read-only dashboard using the local helper:

```bash
scripts/start_delta_research_dashboard.sh
```

Then open:

```text
http://127.0.0.1:8080/?token=vnedge-demo
```

The homepage is the Delta research view. The older generic multi-venue view is
available only as the secondary **Research Lab** route.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
pytest -q
```

Never commit `.env`, credentials, event tapes, databases, Parquet outputs, or
research journals. API credentials, if ever used in a separately reviewed
future path, must be trade-only with withdrawals disabled.

## Repository map

```text
configs/                         frozen runtime and research contracts
docs/                            architecture, contracts, and results
src/vnedge/scalping/delta_engine Delta causal context and research engines
src/vnedge/exchange/             public recorder and isolated adapters
src/vnedge/replay/               deterministic event-time replay
src/vnedge/dashboard/            read-only dashboard and evidence endpoints
src/vnedge/governance/           policy and proof primitives
tests/                            causality, safety, replay, and UI contracts
```

Historical multi-venue/Freqtrade ambitions are retained only in dated design
documents and generic research modules. They do not define the current
VNEdge_Delta product identity.
