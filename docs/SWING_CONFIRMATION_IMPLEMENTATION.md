# Incremental Swing Confirmation

## Status

The submitted swing-confirmation design has been implemented as an additive,
research-only streaming component. It is not connected to the rejected
`continuous_mtf_alignment_v2` scanner and does not change its recorded result.

## Implemented contract

- One tracker instance per symbol and timeframe.
- A pivot becomes visible only after all configured right-hand candles close.
- Input timestamps must be unique and strictly increasing.
- Pivot and confirmation indices are absolute and remain stable when the
  bounded candle window rolls forward.
- Swing strength is the submitted local candle range divided by candle mid,
  expressed in bps.
- Consecutive same-type swings retain only the more extreme swing in the active
  structural sequence.
- Every accepted confirmation is also retained in a bounded archive so active
  replacement does not erase the audit trail.
- The active sequence can be adapted to the existing close-only BOS/CHOCH
  functions through `confirmed_for_structure()`.

## Corrections to the submitted sample

1. The sample stored a deque-relative index, which changes meaning after deque
   rollover. The implementation stores absolute pivot and confirmation indices.
2. The sample appended a candidate to `newly_confirmed` even when `_add_swing`
   rejected it as a weaker duplicate. The implementation only returns accepted
   confirmations.
3. Replacing an active same-type swing removed evidence of the prior confirmed
   swing. The implementation keeps a separate confirmation archive.
4. Pending fields and NumPy were unused and were removed.
5. Wrong-timeframe and regressing-timestamp inputs fail closed.

## Important semantic distinction

The streaming component's `min_swing_bps` follows the submitted definition:

```text
(pivot candle high - pivot candle low) / pivot candle mid * 10,000
```

The frozen v2 experiment used a different rule: excursion from the latest
accepted opposite swing. These definitions are not interchangeable. Connecting
this tracker to a scanner would therefore require a new preregistered experiment
version and a new selection-only replay. It must not be silently substituted
into v2.

## Safety

- No order path was added.
- No scanner or runtime configuration was enabled.
- No historical or untouched backtest interval was opened.
- `can_trade` and `can_promote` remain false.

## Verification

- Focused swing tests: 13 passed with the existing mechanical-structure tests.
- Full repository suite: 1,992 passed, with one unrelated third-party
  deprecation warning.
