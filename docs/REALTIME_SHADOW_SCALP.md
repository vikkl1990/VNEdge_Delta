# Real-time shadow scalp

`vnedge.runtime.realtime_shadow_scalp`

## What it is

A **real-time shadow** runner. It drives the SAME detectors as the
`cascade_reversion` and `leadlag_echo_scalp` research families — but against the
**live tick stream** instead of a recorded tape. The instant a setup forms it
fires a *shadow intent* (never an order), tracks the open virtual scalp, and
resolves it into virtual PnL (`taker_net` + `maker_net`) as subsequent live ticks
arrive.

It is **not trading**. There is no order path, no `PreTradeRiskGateway` call, no
`OrderManager`, no credentials, and no private streams — only public websocket
data. `can_trade` and `can_promote` are `False` on every payload, every lane,
and the policy block. It accumulates evidence; it never acts on it.

Two families, keyed to the streams the recorders already carry:

| family | leader / trigger | resolves on | maker leg |
| --- | --- | --- | --- |
| `cascade` | Binance USDM liquidation stream (forced-order side) + venue trade tape | the venue trade tape | resting entry at the reversion print (`assumed_maker_fill`) |
| `leadlag_echo` | Binance USDM trade tape (leader impulse) | Delta India native L2 book + trades (follower) | queue-aware fill at the follower touch (`assumed_queue_fill`) |

## Why it accelerates evidence

The scalp families detect **100–350× more events** than the sparse 1h lanes ever
fire. But they only ran as periodic **6h batch replay** over recorded data, so a
lane sat at `UNDER_SAMPLED` for as long as it took recordings to accumulate. This
runner fires at the scalp's native timescale (seconds), so the same lane crosses
the `min_events_for_candidate` threshold — and surfaces its **maker-vs-taker cost
split** — in *days*, live.

That is the only thing real-time changes: **evidence velocity, not the gates.**

## One detector implementation, shared with batch

The runner imports the batch detectors (`CascadeDetector`,
`LeaderImpulseDetector`) and the batch replayers' resolution helpers
(`CascadeReversionReplayer` / `EchoScalpReplayer`) and drives them tick-by-tick.
Live firing and batch replay therefore share **one** implementation — a
regression test asserts the live lane and the batch replayer emit identical rows
on the same tape. Stop-wins-ties, the exhaustion/quiet protocol, the queue-aware
maker FIFO, and the dual cost models are exactly the replay families' code.

## Journals, restart, aggregation

Each lane journals to
`logs/scalp_shadow/<family>_<venue>_<symbol>.journal.jsonl`
(`scalp_shadow_intent` at entry, `scalp_shadow_outcome` at resolution). The
journal is the durable store:

- on restart, already-resolved `intent_key`s are skipped (never re-resolved),
- open (unresolved) scalps are rebuilt from the journal and resolved forward
  against subsequent live ticks — the shadow-prime pattern, no double-counting.

The aggregate (virtual trades, `taker_net`, `maker_net`, win rate, PF,
events/hour, verdict) is published to
`research/live_research/realtime_shadow_scalp.json` and folded into the
continuous-research `latest.json` the dashboard reads. The scalp journals also
feed `shadow_perf_reader` (opt-in `scalp_journal_dir`), so the scalp virtual
track record joins the existing live shadow-perf surface with the conservative
**taker** number as its `virtual_net_usd`.

## The maker-fill caveat (read this before believing a maker number)

The **taker** numbers are the honest, always-fills floor. The **maker** numbers
ASSUME a passive fill:

- cascade `maker_first`: a resting entry that filled at the reversion print
  price with no entry slippage — flagged `assumed_maker_fill`;
- echo `maker_first`: a queue-aware fill at the follower touch, filled only once
  live follower trades clear the size displayed ahead of it — flagged
  `assumed_queue_fill`.

Those are **hypotheses**, not evidence. A `maker_net > taker_net` result is a
prompt to do L2 queue replay, not a green light. Cross-venue timestamp alignment
(Binance leader vs Delta follower, two clocks) is a research estimate, not an
execution guarantee.

## Promotion is unchanged

Real-time firing changes how fast a lane accumulates a track record. It does
**not** change what promotion requires. A live `UNDER_SAMPLED → CANDIDATE`
transition here is still only a candidate. Promotion still requires:

1. the replay family's **pre-registered judgment on untouched data**, through the
   same frozen promotion gates, and
2. **human approval**,

with live capital still gated by the full pre-live checklist. Nothing here
auto-promotes, auto-tunes, or trades.

## Running

```
python -m vnedge.runtime.realtime_shadow_scalp            # live public feeds
python -m vnedge.runtime.realtime_shadow_scalp --once     # publish one snapshot
```

Deploy: the `realtime-shadow-scalp` service in `docker-compose.yml`
(`restart: unless-stopped`, public streams only, no credentials mounted, log
caps). Env knobs: `SCALP_CASCADE_VENUE`, `SCALP_CASCADE_SYMBOLS`,
`SCALP_ECHO_PAIRS`, `SCALP_NOTIONAL_USD`, `SCALP_PUBLISH_INTERVAL_SECONDS`, plus
the detectors' own `CASCADE_*` / `ECHO_*` env vars.
