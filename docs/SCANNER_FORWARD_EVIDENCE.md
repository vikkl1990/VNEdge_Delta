# Scanner forward evidence

Status: research-only. No order route exists in this pipeline.

## What it records

The Delta public-candle scanner journals a fresh MTF/AMF alert once using a
stable identity derived from scanner, market, signal candle, and side. Each
`scanner_alert` record contains:

- market and side;
- completed signal-candle time;
- next 1h candle open and its timestamp;
- optional L2 weighted OBI and 5-second trade-flow context;
- explicit `can_trade=false` and `can_promote=false` flags.

On every refresh, already-journaled identities are skipped. The append-only
journal is `research/live_research/mtf_amf_alerts.jsonl`.

## Forward labels

Only subsequently completed 1h candles resolve outcomes. The tracker publishes
MFE, MAE, gross return, and net return after a fixed 12 bps round trip at 1h,
4h, 12h, and 24h. A false signal is a resolved horizon with net return at or
below zero. The dashboard reports lifetime and rolling-last-30 expectancy,
profit factor, false-signal rate, and market breakdown.

L2 data is context only. It cannot create, filter, size, execute, or promote a
signal. Missing L2 data produces `unavailable`; the candle alert remains
unchanged.

## Expanded causal backtest

Run the unchanged scanner thresholds over the liquid crypto-perpetual universe:

```bash
python -m vnedge.research.scanner_forward_evidence \
  --symbols BTCUSD,ETHUSD,SOLUSD,XRPUSD,BNBUSD,DOGEUSD,LINKUSD,AAVEUSD \
  --days 590 \
  --start 2025-01-01T00:00:00+00:00
```

Pre-2025 candles are warm-up only. Signals use completed 1h candles and only
completed 4h candles available at the signal time. Entries use the next 1h
open. The last 25% of the common 2025-to-current window is untouched; the best
horizon is selected using development data only and then frozen for untouched
judgment.

The command writes the full JSON report plus daily, weekly, monthly, and
quarterly CSV summaries for the development-selected horizon beside it.

## Locked paper-review gates

- positive untouched expectancy in at least two markets;
- untouched profit factor above 1.2 after 12 bps costs;
- no market contributes more than 60% of positive untouched net bps;
- no month contributes more than 40% of positive untouched net bps;
- at least 30 untouched outcomes;
- causal, non-repainting features;
- completed candles only.

Passing means `ELIGIBLE_FOR_PAPER_REVIEW`, not automatic paper activation.
Paper remains off until all gates pass and a human approves the trial. Live
trading is outside this module.

## Dashboard

`GET /scanner-evidence` serves the read-only artifact. The local dashboard shows
rolling expectancy, profit factor, false-signal rate, market breakdown, and each
locked gate next to the live signal tape.
