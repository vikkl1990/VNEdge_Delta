# GitHub Bitcoin Topic Deep Dive

Snapshot date: 2026-07-09.

Source: [GitHub topics/bitcoin](https://github.com/topics/bitcoin).

The Bitcoin topic is different from the broader crypto/trading topic. The most
important repositories are not signal bots. They are protocol, node, wallet,
mempool, Lightning, payment, and infrastructure projects.

For VNEDGE, the useful conclusion is:

```text
Bitcoin topic research should feed BTC regime intelligence,
not scalper order generation.
```

BTC perpetual strategies can fail when the exchange tape looks normal but the
Bitcoin network, fee market, miner behavior, or liquidity regime is changing
underneath it. The right build is a read-only Bitcoin regime layer that feeds
research, Alpha Council, and dashboard context.

## Source Set

Primary repositories inspected:

| Project | What matters for VNEDGE |
|---|---|
| [bitcoin/bitcoin](https://github.com/bitcoin/bitcoin) | Bitcoin Core reference implementation. Useful for node/RPC reliability, chain height, mempool, block, fee, and validation-health context. |
| [mempool/mempool](https://github.com/mempool/mempool) | Mempool visualizer, explorer, and API focused on Bitcoin's transaction fee market. Strong reference for mempool stress and fee-market telemetry. |
| [lightningnetwork/lnd](https://github.com/lightningnetwork/lnd) | Lightning node with channel state, graph, routing, and liquidity concepts. Useful later for Lightning/liquidity stress context, not immediate scalping. |
| [ElementsProject/lightning](https://github.com/ElementsProject/lightning) | Core Lightning, spec-compliant implementation with JSON-RPC and plugin orientation. Useful operational pattern for Unix-socket style local services and node health checks. |
| [btcsuite/btcd](https://github.com/btcsuite/btcd) | Alternative Bitcoin full node in Go. Useful as a clean separation example: node without wallet by design. |
| [rust-bitcoin/rust-bitcoin](https://github.com/rust-bitcoin/rust-bitcoin) | Bitcoin data-structure library. Important caution: not for consensus validation. Good lesson for library boundaries. |
| [spesmilo/electrum](https://github.com/spesmilo/electrum) | Lightweight Bitcoin wallet. Useful for security/dependency lessons, not a trading dependency. |
| [btcpayserver/btcpayserver](https://github.com/btcpayserver/btcpayserver) | Self-hosted Bitcoin payment processor with Lightning support and API surface. Useful for self-hosted, non-custodial operational patterns. |
| [bisq-network/bisq](https://github.com/bisq-network/bisq) | Decentralized bitcoin exchange network. Useful as market-structure context, not a perp execution venue. |
| [askmike/gekko](https://github.com/askmike/gekko) | Older Bitcoin trading bot. Stale as architecture guidance for VNEDGE. |
| [michaelgrosner/tribeca](https://github.com/michaelgrosner/tribeca) | Older market-making crypto platform. Useful historically, but not enough to override current VNEDGE maker/replay design. |
| [ctubio/Krypto-trading-bot](https://github.com/ctubio/Krypto-trading-bot) | C++ market-making bot. Worth noting, but not the main Bitcoin-topic lesson. |

## Executive Verdict

The Bitcoin topic does not reveal a hidden scalper indicator package. It
reveals a missing regime layer.

VNEDGE already studies exchange-local data:

- candles
- public trades
- L2/top-of-book
- funding
- cross-venue lead-lag
- replay and shadow evidence

The Bitcoin topic adds a different information plane:

- block production rhythm
- mempool congestion
- fee-market pressure
- miner revenue stress
- on-chain settlement pressure
- node sync health
- Lightning/channel liquidity stress
- self-hosted data sovereignty

These should be used as context, gates, and research splits. They should not
directly fire scalper entries.

## What To Borrow

### 1. Bitcoin Core as a read-only truth source

Bitcoin Core is security-critical software with a mature testing and review
culture. For VNEDGE, the important interface is read-only RPC, not wallet
usage.

Useful data:

- `getblockchaininfo`
- current block height
- node sync status
- headers height
- verification progress
- block time drift
- `getmempoolinfo`
- mempool transaction count
- mempool virtual size
- mempool memory usage
- `estimatesmartfee`
- fee-rate buckets
- `getchaintips`
- reorg/stale-tip awareness

VNEDGE use:

- BTC regime labels
- node health telemetry
- data-quality gate for on-chain features
- dashboard network-health panel
- Alpha Council context rows

Hard rule:

```text
Bitcoin Core wallet RPC must not be used by VNEDGE.
```

Read-only node telemetry is useful. Custody is out of scope.

### 2. Mempool.space as fee-market intelligence

The mempool project is explicitly focused on Bitcoin's evolving transaction fee
market. That is relevant for BTC perps because fee spikes and mempool stress
often coincide with volatility, panic settlement, exchange inflows/outflows, or
news-driven movement.

Useful features:

- mempool transaction count
- mempool vsize
- fastest/half-hour/hour fee estimates
- fee histogram
- block fullness
- recent block intervals
- unconfirmed transaction pressure
- fee spike z-score
- congestion state: calm / building / stressed / panic

VNEDGE use:

- context split for BTC scalper replay
- context split for BTC funding mean reversion
- risk throttle during fee-market panic
- dashboard "Bitcoin Network Stress" panel
- Alpha Council regime evidence

Important restraint:

```text
Mempool stress is not an entry signal by itself.
```

It is a condition label. The research question is whether existing signals
behave differently under fee-market stress.

### 3. Lightning as liquidity-stress context

LND and Core Lightning show a mature operational model around channels, graph
state, routing, fees, and node health.

Immediate VNEDGE use is limited. Lightning data is not needed before we have
basic Bitcoin node and mempool features.

Future research features:

- public channel count trend
- public channel capacity trend
- routing fee changes
- node graph churn
- channel open/close bursts
- Lightning-related on-chain activity

Likely usage:

- slow-loop BTC regime context
- not scalper trigger
- not execution dependency

### 4. Self-hosted infrastructure discipline

Bitcoin Core, mempool, BTCPay, Electrum, and Lightning projects all converge on
one operational pattern: self-hosted, verifiable, security-conscious services.

VNEDGE should copy the posture:

- no third-party dependency for critical telemetry when self-hosting is easy
- read-only service credentials
- explicit health checks
- version/provenance metadata
- restart-safe local state
- API isolation
- no trading keys near Bitcoin node services

### 5. Wallet and payment projects as security references

Electrum and BTCPay are not trading components for VNEDGE. Their value is in
security and ops design:

- strict dependency awareness
- hardware-wallet separation
- non-custodial posture
- API boundaries
- self-hosted deployments
- test/regtest patterns
- cautious release verification

VNEDGE should not add wallet features. A trading bot should avoid becoming a
custody system.

## What Not To Copy

Do not copy these patterns into VNEDGE:

- wallet/custody functionality
- Bitcoin private-key handling
- payment processing
- Lightning channel management
- P2P exchange execution
- stale trading bot architecture from older Bitcoin repos
- protocol libraries as consensus validators
- on-chain metrics as direct scalper triggers

Also do not make Bitcoin node health a live-order dependency. If the node is
down, on-chain features become unavailable and should be marked `missing`, but
exchange execution safety remains governed by VNEDGE's existing risk and data
quality gates.

## VNEDGE Gap Map

| Area | Current state | Gap from Bitcoin topic | Build direction |
|---|---|---|---|
| BTC regime | Exchange-local context exists | No native Bitcoin network/fee/mempool regime | Add BTC regime sensor |
| On-chain telemetry | Not first-class | No node/mempool feature artifact | Add read-only Bitcoin node collector |
| Fee-market context | Funding/fees at exchange level | No Bitcoin network fee stress feature | Add mempool stress model |
| Alpha Council | Debates existing research artifacts | No Bitcoin-network context rows | Add regime evidence producer |
| Dashboard | Exchange and signal focus | No Bitcoin network stress panel | Add read-only network panel |
| Scalper replay | L2 and orderflow oriented | No split by mempool stress | Tag replay windows by BTC regime |
| Risk throttles | Account/exchange-risk oriented | No context throttle for network panic | Add optional research-only throttle proposal |
| Infra provenance | Docker/VM work ongoing | No self-hosted Bitcoin node plan | Add optional node/mempool deployment doc |

## Bitcoin Regime Sensor

The highest-value build inspired by this topic is:

```text
vnedge.research.bitcoin_regime
```

It should be a read-only producer. No secrets, no orders, no wallet RPC.

Inputs:

- Bitcoin Core RPC, if configured
- self-hosted mempool API, if configured
- public mempool API only as fallback
- local cached artifacts

Outputs:

```text
research/live_research/bitcoin_regime_latest.json
research/live_research/bitcoin_regime_history.jsonl
```

Example payload:

```json
{
  "schema_version": "bitcoin_regime_v1",
  "as_of": "2026-07-09T00:00:00Z",
  "source": "bitcoin_core_rpc",
  "can_trade": false,
  "can_promote": false,
  "node": {
    "synced": true,
    "block_height": 900000,
    "headers": 900000,
    "verification_progress": 0.99999
  },
  "mempool": {
    "tx_count": 124000,
    "vsize_vb": 480000000,
    "min_fee_sat_vb": 2.1,
    "fastest_fee_sat_vb": 55,
    "stress_state": "stressed"
  },
  "features": {
    "fee_spike_z": 2.4,
    "block_interval_z": 1.1,
    "mempool_pressure_z": 2.8
  },
  "research_tags": [
    "btc_fee_market_stressed",
    "mempool_pressure_high"
  ]
}
```

Every payload remains research/context only:

- `can_trade=false`
- `can_promote=false`
- no direct strategy signal

## How This Helps Scalping

The scalper is currently struggling because exchange-local microstructure
signals are often zero-edge after costs.

Bitcoin regime features can help in three ways:

1. Filter when not to scalp BTC.
2. Split replay results into context buckets.
3. Find event windows where exchange microstructure has conditional edge.

Examples:

| Regime tag | Possible research question |
|---|---|
| `fee_market_panic` | Does BTC lead-lag become stronger when on-chain settlement is congested? |
| `mempool_calm` | Are maker fills less toxic when fee pressure is low? |
| `block_interval_slow` | Does volatility rise after delayed blocks during high-fee windows? |
| `mempool_pressure_rising` | Do breakout/funding strategies improve when settlement demand rises? |
| `node_data_missing` | Should on-chain-conditioned lanes be excluded from judgment? |

The goal is not more signals. The goal is better conditional evidence.

## How This Helps Swing/Daily Lanes

Bitcoin topic data is likely more useful for slow and medium loops than pure
scalping.

Candidate slow-loop features:

- fee pressure percentile
- mempool congestion duration
- block production surprise
- miner fee revenue proxy
- on-chain settlement stress
- exchange-local funding combined with network stress
- BTC dominance or BTC/ETH relative strength from market data

Research usage:

```text
BTC funding-MR
+ fee-market stress tag
+ funding percentile
+ volatility regime
+ 4h / 1h trend context
```

This may explain why an otherwise good candle strategy should stand down in
some regimes.

## Dashboard Implication

Add a read-only Bitcoin Network panel to the cockpit:

- node synced / not synced
- block height
- mempool tx count
- vsize
- fastest fee
- fee spike z-score
- stress state
- latest regime tags
- last artifact update
- data source: local node / self-hosted mempool / public fallback / missing

This panel should be informational. No trade controls.

## Alpha Council Implication

The Alpha Council should ingest Bitcoin regime artifacts as context rows.

Agent roles:

- `edge_advocate`: identifies regimes where existing candidates improved
- `skeptic`: blocks conclusions from tiny context buckets
- `execution_specialist`: checks whether fee-market stress worsens maker fill
  toxicity
- `risk_governor`: prevents direct promotion from on-chain context
- `research_director`: creates replay split tasks

New workbench tasks:

- `BACKFILL_BTC_REGIME`
- `SPLIT_REPLAY_BY_BTC_REGIME`
- `CHECK_MEMPOOL_STRESS_EDGE`
- `REFRESH_BITCOIN_NODE_HEALTH`

## Recommended PR Sequence

### PR 1 - Bitcoin Regime Sensor

Branch:

```text
codex/bitcoin-regime-sensor
```

Build:

- read-only Bitcoin Core RPC client
- optional mempool API client
- local artifact writer
- stress-state classifier
- tests with static fixtures
- no wallet RPC
- no execution dependency

### PR 2 - Replay Context Tags

Branch:

```text
codex/bitcoin-regime-replay-tags
```

Build:

- join BTC regime artifacts to replay windows
- produce per-regime replay summaries
- expose "edge improves under X regime?" report
- Alpha Council task integration

### PR 3 - Cockpit Bitcoin Network Panel

Branch:

```text
codex/bitcoin-network-cockpit-panel
```

Build:

- dashboard state endpoint includes Bitcoin regime
- terminal panel for network stress
- stale/missing indicators
- no controls

### PR 4 - Self-Hosted Node Deployment Notes

Branch:

```text
codex/bitcoin-node-deploy-notes
```

Build:

- optional `bitcoind` read-only RPC setup notes
- optional mempool self-host notes
- security checklist
- VM resource warnings
- no wallet setup

## Bottom Line

The Bitcoin topic does not make VNEDGE a better bot by adding Bitcoin wallet,
payment, or Lightning execution code.

It makes VNEDGE better by adding Bitcoin-native context:

```text
Bitcoin node health
+ mempool fee stress
+ block rhythm
+ settlement pressure
+ exchange microstructure
+ funding
+ replay evidence
```

That is the right way to use Bitcoin infrastructure in a derivatives trading
assistant: as a regime and research layer, never as a shortcut around the
execution and risk gates.
