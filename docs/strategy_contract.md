# Strategy contract

Every strategy in this system — hand-written or AI-authored — implements the
same interface (`vnedge.strategy.base_strategy.BaseStrategy`). The contract
exists to make one property structural rather than aspirational: **a strategy
can never trade on information it could not have had live.** The backtester,
paper engine, and live engine all drive strategies through this contract, so a
strategy that respects it behaves identically across all of them.

This is the document an author (human or model) writes to. AI-authored
strategies additionally pass the sandbox in `docs/AI_SANDBOX.md`, but the
behavioural contract below is the same for everyone.

## The two methods

```python
class MyStrategy(BaseStrategy):
    strategy_id = "my_strategy"          # required, non-empty string
    warmup_bars = 50                     # bars needed before signal() is valid

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()              # never mutate the input frame
        df["feature"] = sma(df["close"], 20)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]             # read rows <= index ONLY
        ...
        return SignalIntent(side="long", stop_price=..., take_profit_price=...)
```

### `prepare(candles) -> DataFrame`

- Returns a **copy** of `candles` with indicator columns added. It must not
  mutate the input frame.
- May use vectorized operations over the whole frame, but **only causal ones**:
  rolling windows and **backward** shifts. A value at row *i* may depend on
  rows `0..i` only — never on any row `> i`.
- Input columns are the canonical OHLCV set: `timestamp, open, high, low,
  close, volume` (see `vnedge.data.schemas`).

### `signal(df, index) -> SignalIntent | None`

- Called at the **close** of bar `index`. It may read rows `0..index` only.
- Returns a `SignalIntent` to enter, or `None` for no action.
- The engine fills any resulting intent at the **open of bar `index + 1`** — a
  strategy never trades on the bar it decided in.
- **Every intent must carry a positive `stop_price`.** Stop-less intents raise
  in `SignalIntent.__post_init__` — they are not representable by design.

## Causality rules (the load-bearing part)

1. **Rows ≤ index.** In `signal`, never look at `df.iloc[index + k]` for `k > 0`,
   and never index by `index + 1`, `i + 1`, etc.
2. **Backward shifts only.** `series.shift(1)` (previous bar) is causal.
   `series.shift(-1)` (next bar) is lookahead and is rejected.
3. **No whole-series reductions leaking the future.** `df["close"].max()`,
   `.iloc[-1]`, `.tail(...)`, or any statistic computed over the full frame and
   written back onto earlier rows leaks the end of the series into the past.
   Use trailing rolling windows (`.rolling(n)`, `.expanding()` is fine because
   it only sees the past) instead.
4. **NaN is the warmup marker.** Indicators emit `NaN` until their window fills;
   `signal` must treat any `NaN` input as "no signal" and return `None`. Set
   `warmup_bars` to the first index at which all features are defined.
5. **`prepare` must be truncation-invariant.** Running `prepare`+`signal` on the
   first *k* bars must reproduce exactly what the full-series run produced for
   those same bars. This is machine-checked — see below.

### Machine check

`vnedge.research.causality_analyzer.analyze_strategy` proves truncation
invariance: it runs the strategy on the full series and on truncated prefixes
and flags any feature or signal that changes in the past when the future is
chopped off. Every registered strategy is covered by
`tests/test_causality_all_strategies.py`; AI strategies are checked by
`ai_candidate_research` before any walk-forward runs. A strategy that fails this
check is refused, not fixed-up.

Honest scope: the analyzer proves invariance of the code paths exercised on the
given data, not full branch coverage. It runs on a deterministic synthetic
market by default and can re-run on real exchange data via its CLI.

## Explainability

`SignalIntent.reason` is a human-readable trigger description
(`"fast SMA(12) crossed above slow SMA(48) at 51234.50"`). It is a feature, not
decoration: rejections and decisions are meant to be explainable end-to-end.

## Allowed building blocks

- Causal indicators live in `vnedge.strategy.indicators` (`sma`, `ema`, `atr`,
  `prior_high`/`prior_low` — current bar excluded, `zscore`,
  `rolling_percentile`, `efficiency_ratio`). Prefer these; they are unit-tested
  for causality.
- `pandas` / `numpy` / `math` for arithmetic.

## Prohibitions

- No filesystem, network, process, or reflection access from strategy code.
- No mutation of the input candle frame.
- No future access of any kind (rules 1–3 above).
- No side effects: `prepare`/`signal` compute and return; they do not write
  files, spawn threads, or touch global state.

For **AI-authored** strategies these prohibitions are enforced mechanically by
the sandbox validator (deny-by-default AST allowlist + restricted execution) —
see `docs/AI_SANDBOX.md`. Hand-written strategies are held to the same contract
by review and by the causality analyzer.

## What a strategy is NOT

A strategy produces stop-carrying entry intents from causal features. It does
**not** size positions (that is risk-based, in `risk/position_sizer.py`), decide
leverage, place or manage orders, or bypass any gate. Passing this contract and
the promotion gates makes a strategy a *candidate*; a pre-registered judgment on
untouched data and explicit human approval are still required before it trades.
