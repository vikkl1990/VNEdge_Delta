# Crypto-Signal Review For VNEDGE

Reviewed: https://github.com/CryptoSignal/Crypto-Signal

## What It Is

Crypto-Signal is a broad technical-analysis scanner. It loops through configured
market pairs, exchanges, indicators, informants, and crossovers, then emits
hot/cold/neutral alerts through pluggable notifiers.

Useful design primitives:

- Config-driven market-pair sweeps across many symbols and venues.
- Dynamic analyzer dispatchers for indicators, informants, and crossovers.
- Hot/cold signal status that is easy for operators to scan.
- Alert templating with exchange, market, analyzer, status, and raw values.
- Multiple notifier outputs: Telegram, Discord, Slack, email, webhook, stdout.

## What We Should Not Copy

- TA-only edge logic. RSI/MACD/VWAP/Ichimoku can be features, but they are not a
  scalper edge by themselves in 2026.
- No capital-protection spine. Crypto-Signal alerts; it does not enforce a
  pre-trade risk gateway, mode ladder, idempotent order manager, reconciliation,
  or journal-before-submit.
- No promotion discipline. There is no walk-forward, untouched judgment,
  shadow/paper ladder, or fee-wall proof contract.
- No microstructure proof. It does not solve maker/taker routing, queue fills,
  adverse selection, or L2 replay.

## VNEDGE Product Lessons

The UI lesson is strong: scanners need to be visually legible. Operators should
not read a large table to learn whether a lane is hot, cold, blocked, or
waiting.

Adopted into the cockpit polish pass:

- A denser terminal-style market tape.
- Signal pressure cards before the raw funnel table.
- Hot/cold-style funnel status: evaluated, fired, approved, rejected,
  submitted, filled, and virtual net.
- More compact first-viewport hierarchy so the operator sees state, gates,
  market tape, and signal pressure faster.

Future backend candidates inspired by the review:

- A safe analyzer-registry surface for feature-only scanners.
- Alert templates for proof tasks and signal-funnel anomalies.
- A scanner-health view grouped by exchange, pair, timeframe, and feature family.

Non-negotiable boundary: scanner output can propose research work only. It never
bypasses VNEDGE's risk gateway, proof ladder, replay gates, or live-order locks.
