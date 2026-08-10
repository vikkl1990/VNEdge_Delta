# Bitcoin Regime Sensor

`vnedge.research.bitcoin_regime` is a read-only producer for BTC-native market
context:

- Bitcoin Core sync/chain health from safe RPC calls
- mempool size and fee pressure
- mempool.space-compatible recommended fees
- short history z-scores for fee and mempool spikes

The output is written to:

- `research/live_research/bitcoin_regime_latest.json`
- `research/live_research/bitcoin_regime_history.jsonl`

## Safety Contract

The sensor is research-only:

- `can_trade=false`
- `can_promote=false`
- `orders_allowed=false`
- `wallet_rpc_allowed=false`
- no exchange credentials
- no wallet, send, sign, import, dump, fund, or generate RPCs

The JSON-RPC client has an explicit allowlist:

- `getblockchaininfo`
- `getmempoolinfo`
- `estimatesmartfee`
- `getchaintips`

## How The Council Uses It

Calm/healthy network state is ignored. Non-calm or unhealthy state becomes a
context candidate in the Alpha Council:

- `SPLIT_REPLAY_BY_BTC_REGIME` when the source is healthy and the fee market is
  building, stressed, or panic
- `REFRESH_BITCOIN_NODE_HEALTH` when the source is missing, partial, errored,
  or unsynced

This does not create signals. It creates the next proof task: split replay and
research reports by Bitcoin network regime so we can measure whether an alpha
only works in certain BTC-native stress states.

## Run

One-shot with public mempool API:

```bash
python -m vnedge.research.bitcoin_regime \
  --mempool-api-base https://mempool.space \
  --once --json
```

With a read-only local Bitcoin Core RPC:

```bash
BITCOIN_RPC_URL=http://127.0.0.1:8332 \
BITCOIN_RPC_USER=... \
BITCOIN_RPC_PASSWORD=... \
python -m vnedge.research.bitcoin_regime --once
```

Docker Compose:

```bash
docker compose up -d --build bitcoin-regime-sensor alpha-council alpha-workbench
```
