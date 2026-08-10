# External Signal Intake

`external_tradingview_signal_v1` is the VNEDGE contract for TradingView-style
webhook alerts from manual indicator stacks such as Trader Assistant Pro or FVG
Engine. It is not an execution shortcut.

## What It Accepts

Expected JSON fields:

- `event`: must be `trade_opened`
- `ticker`: for example `BTCUSDT`
- `tf`: alert timeframe
- `direction`: `LONG` or `SHORT`
- `stage`: default floor is `3`
- `score`: default floor is `60`
- `confluence`: default floor is `3`
- `entry`
- `sl`
- `tp1`, `tp2`, `tp3`

The validator rejects weak or unsafe alerts before they become internal
candidates:

- wrong event type
- missing ticker/timeframe
- invalid side
- stage/score/confluence below floor
- invalid long/short price geometry
- missing take profit
- reward-to-risk below floor
- current market price too far from alert entry

## Execution Safety

The intake module converts a valid alert into a normal `SignalCandidate` with
strategy id `external_tradingview_signal_v1`.

Default behavior is deliberately blocked:

```text
source_verified = false
route = BLOCKED
expected_edge_bps = 0
```

That means an external alert can be stored, inspected, shadowed, and reported,
but the `SignalArbiter` will reject it. A source can only become selectable
after VNEDGE supplies source-level evidence such as replay or paper/shadow
performance:

```python
ExternalSignalPolicy(
    source_verified=True,
    verified_expected_edge_bps=14.0,
    verified_profit_factor=1.9,
    verified_route="MAKER_ONLY",
)
```

Even then, this module does not submit orders. The selected signal still flows
through the normal VNEDGE chain:

```text
SignalArbiter -> position sizing -> PreTradeRiskGateway -> DecisionJournal
-> OrderManager -> adapter -> reconciliation
```

## TP Splits

Willy-style bot execution commonly uses partial take profits and breakeven
movement after TP1. VNEDGE currently carries that as metadata only:

```text
60% TP1 / 20% TP2 / 10% TP3 / 10% runner after breakeven
```

The current `SignalIntent` supports one target, so the candidate uses TP3 as
the single target and stores the full split under:

```text
candidate.metadata.exit_plan
```

Live partial exits require a separate order-manager feature. Until then, the
metadata is for audit, shadow comparison, and future implementation.

## Why This Exists

The useful idea from external indicator products is not their claimed win rate.
The useful idea is disciplined automation:

- structured alerts instead of screenshots
- fixed entry/stop/target plan
- position sizing from stop distance
- no trade after loss streaks
- no moving stop against the position
- full action logs and reports

VNEDGE already enforces the risk and journal side. This module adds the missing
safe intake contract while keeping capital protected.
