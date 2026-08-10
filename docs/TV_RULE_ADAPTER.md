# Local TradingView Rule Adapter

`tv_rule_adapter_v1` is the safe execution boundary between lawfully supplied
Pine source and VNEDGE research.

It is deliberately **not** an integration with the unofficial `tvscreener`
network client. It makes no TradingView request, ingests no TradingView market
data, and never reads protected or invite-only scripts. The open-source
`tvscreener` project is useful as a reference for field naming and typed query
design, but VNEDGE computes every feature from its own Delta candle data.

## Flow

```text
user-supplied / permissively licensed Pine source
                    |
                    v
        provenance + source hash check
                    |
                    v
     safe-subset parser and repaint quarantine
                    |
                    v
       immutable vnedge.tv_rule_spec.v1
                    |
                    v
        local closed Delta OHLCV evaluation
                    |
                    v
       research signal frame + source-bound artifact
```

There is no link from this adapter to `strategy_registry`, the paper broker,
the risk gateway, or an order route. Every artifact has `can_trade=false` and
`can_promote=false`.

## Supported Pine subset

- Immutable assignments and non-negative history references such as `high[1]`.
- Boolean/arithmetic expressions using `and`, `or`, `not`, comparisons, and
  ordinary arithmetic.
- `input.int`, `input.float`, `input.bool`, and `input.source` defaults.
- `ta.sma`, `ta.ema`, `ta.rsi`, `ta.atr`, `ta.highest`, `ta.lowest`,
  `ta.roc`, `ta.stdev`, `ta.crossover`, and `ta.crossunder`.
- Direction contracts from guarded `strategy.entry` calls,
  `alertcondition`, or clearly named `long_signal` / `short_signal`
  assignments.
- Basic `strategy.exit` stop/limit/profit/loss/trailing expressions as
  metadata for later manual port review.

Unsupported functions, mutable state, loops, arrays, custom functions,
multi-timeframe `request.security`, future references, `lookahead_on`,
real-time-only state, and complex order lifecycle calls fail closed.

This is intentionally conservative. Complex Pine must be manually translated
into a VNEDGE-owned strategy and then pass the normal causal test suite.

## Compile only

```bash
python -m vnedge.research.tv_rule_adapter \
  --source research/pine_scripts/sources/my_rule.pine \
  --title "My Rule" \
  --timeframe 5m \
  --license MPL-2.0 \
  --provenance public_open_source
```

The default artifact is:

```text
research/live_research/tv_rule_adapter_latest.json
```

The artifact contains normalized rule expressions and a source SHA-256 hash,
never the full supplied Pine source.

## Compile and evaluate on local Delta candles

The CSV or Parquet file must contain chronological, unique closed candles with
`open`, `high`, `low`, `close`, and `volume`. CSV input may use `timestamp`,
`ts`, `datetime`, or `date` as its index column.

```bash
python -m vnedge.research.tv_rule_adapter \
  --source research/pine_scripts/sources/my_rule.pine \
  --title "My Rule" \
  --timeframe 5m \
  --candles data/delta/BTCUSD_5m.parquet \
  --signals-out research/live_research/my_rule_signals.parquet
```

Evaluation uses only present and past rows. It publishes signal counts and a
deterministic hash so repeat runs can be compared exactly. It does not claim
profitability; signals must still enter the fee-aware chronological backtest
and sealed untouched protocol.

## Dashboard

The authenticated Pine Research Lab exposes the latest artifact at:

```text
/pine-research/rule-spec
```

The page explicitly shows the local-data boundary, blocker count, and zero
TradingView network calls.
