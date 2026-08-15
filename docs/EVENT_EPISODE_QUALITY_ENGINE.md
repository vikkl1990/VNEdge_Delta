# Event Episode Quality Engine

The event episode quality engine is the research gate between raw absorption
detections and any directional or exit study. It never emits a trading
candidate and cannot grant paper, promotion, or live authority.

## Causal funnel

1. Raw detector publications are deduplicated by key.
2. Correlated publications within 30 seconds become one independent episode.
3. The first detection is the immutable anchor. Quality data freezes after
   three seconds; later duplicates cannot improve the score retroactively.
4. Market truth requires causal response snapshots, a valid trade/book join,
   mark/spot basis, and open interest with a causal delta.
5. A frozen score combines aggressive-volume surprise, book
   depletion/replenishment, price-response efficiency, OI/basis dislocation,
   and persistent reclaim/failure behavior.
6. Every episode is offered a nearby control matched by symbol, UTC session,
   and pre-event volatility. The aggregate gate is evaluated only on episodes
   that passed the predeclared abnormal-score threshold.
7. Direction and fee-wall research remain locked unless the selected episodes
   have at least 30 matched pairs, positive mean uplift, and a pair win rate
   above 50%.

## Run

```bash
python -m vnedge.research.event_episode_quality \
  --journal logs/delta_event_research.jsonl \
  --event-root data/delta_events \
  --output research/live_research/event_episode_quality_latest.json \
  --code-version "$(git rev-parse HEAD)"
```

The replay is intentionally heavyweight and is not run inside the five-minute
health heartbeat. The heartbeat reports whether the latest artifact exists;
use `--refresh-quality` on the readiness publisher only for an explicit
one-shot refresh.

The Delta dashboard validates the artifact schema and enforces
`can_trade=false` and `can_promote=false` before presenting its funnel. Invalid
or legacy artifacts are withheld.

AMF v3 remains a separate swing evidence stream with its existing 60-trade
sample requirement. Event episodes never contribute to that sample.
