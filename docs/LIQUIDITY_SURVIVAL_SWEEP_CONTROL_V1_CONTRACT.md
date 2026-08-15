# Liquidity Survival Sweep Control v1 — frozen clean-room contract

`liquidity_survival_sweep_control_v1` tests one question: does a genuine,
abnormal event at a persistent multi-scale liquidity location create more
subsequent movement than an ordinary matched market moment?

It is inspired by public catalogue descriptions of liquidity lifecycle and
zone survival. No Pine source, proprietary formula, or visual signal is copied.

## Point-in-time contract

- Fast and medium levels are unique confirmed 15-minute pivots. Slow levels
  are unique confirmed 1-hour pivots. A level does not exist until all frozen
  right-side confirmation bars have closed.
- Nearby same-side levels are merged within 5 bps. A multi-level sweep needs at
  least two independently confirmed members.
- Level interactions are resolved on completed candles. The online survival
  estimate is a Beta posterior built only from interactions whose resolving
  candle closed before the event decision.
- The abnormal-event and reclaim/failure state comes from recorded public
  trades and L2 market truth. Candle “order flow” is forbidden.
- Direction becomes available after the fixed 3-second Event Episode Quality
  window: reclaim means reversal; failure with persistent flow means
  continuation. Entry basis is the first public trade at or after that time
  plus the frozen 250 ms route delay.

## Economic and evidence gates

- The next active liquidity pool in the confirmed direction must provide at
  least 5× the `taker_full_14_8` route cost.
- `prior_expected_net_bps` is a pre-event structural estimate based on the
  online survival probability, target room, invalidation distance, and route
  cost. It is never presented as calibrated realized edge.
- Selected episodes are compared with earlier symbol/session/volatility
  controls. Each control's full 15-minute outcome must be known before the
  selected episode.
- Exit permutations remain locked until at least 30 control pairs show
  positive average uplift and an outperformance rate above 50%.
- The result stays outside the canonical strategy registry unless selection
  passes. The sealed tail never opens automatically. AMF v3 remains separate.

The exact executable parameters are frozen in
`configs/research/liquidity_survival_sweep_control_v1.yaml`.
