"""Strategy registry — the single place strategies are looked up by name.

Later milestones (approval workflow, live config) reference strategies by
registry name only, so an approved strategy is always a specific, importable
class — never an ad-hoc script.
"""

from __future__ import annotations

from vnedge.strategy.alpha_stack import AlphaStackConfluence
from vnedge.strategy.alpha_distillation_pack import AlphaDistillationPack
from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.context_scalper_v2 import ContextScalperV2
from vnedge.strategy.crypto_trend_atr_margin import CryptoTrendAtrMargin
from vnedge.strategy.datrend_nomada_scalper import DATrendNomadaScalper
from vnedge.strategy.fvg_liquidity_breakout import FvgLiquidityBreakoutScanner
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion
from vnedge.strategy.funding_squeeze_continuation import FundingSqueezeContinuation
from vnedge.strategy.luxara_break_bounce_v27 import LuxaraBreakBounceV27Scanner
from vnedge.strategy.luxara_live_plan_qtm import LuxaraLivePlanQTMScanner
from vnedge.strategy.luxy_ut_bot_forecast import LuxyUTBotForecastScanner
from vnedge.strategy.momentum_cascade_lyro import MomentumCascadeLyroScanner
from vnedge.strategy.panic_reversal import PanicReversal
from vnedge.strategy.quant_signal_pack import QuantSignalPack
from vnedge.strategy.quantified_fee_wall_sniper import QuantifiedFeeWallSniper
from vnedge.strategy.sats_5m_scalper import Sats5mScalper
from vnedge.strategy.smc_playbook_scalper import SMCPlaybookScalper
from vnedge.strategy.stealth_trail_bbp import (
    HumanTradeFingerprintScanner,
    StealthTrailBBPScanner,
)
from vnedge.strategy.trend_continuation import TrendContinuation
from vnedge.strategy.trend_retest import TrendRetest
from vnedge.strategy.vnedge_algo_ml_pro import VNEDGEAlgoMLProScanner
from vnedge.strategy.vol_expansion_breakout import VolatilityExpansionBreakout

STRATEGIES: dict[str, type[BaseStrategy]] = {
    CryptoTrendAtrMargin.strategy_id: CryptoTrendAtrMargin,
    TrendContinuation.strategy_id: TrendContinuation,
    FundingMeanReversion.strategy_id: FundingMeanReversion,
    VolatilityExpansionBreakout.strategy_id: VolatilityExpansionBreakout,
    PanicReversal.strategy_id: PanicReversal,
    FundingSqueezeContinuation.strategy_id: FundingSqueezeContinuation,
    DATrendNomadaScalper.strategy_id: DATrendNomadaScalper,
    FvgLiquidityBreakoutScanner.strategy_id: FvgLiquidityBreakoutScanner,
    AlphaStackConfluence.strategy_id: AlphaStackConfluence,
    QuantSignalPack.strategy_id: QuantSignalPack,
    Sats5mScalper.strategy_id: Sats5mScalper,
    StealthTrailBBPScanner.strategy_id: StealthTrailBBPScanner,
    HumanTradeFingerprintScanner.strategy_id: HumanTradeFingerprintScanner,
    SMCPlaybookScalper.strategy_id: SMCPlaybookScalper,
    TrendRetest.strategy_id: TrendRetest,
    AlphaDistillationPack.strategy_id: AlphaDistillationPack,
    LuxyUTBotForecastScanner.strategy_id: LuxyUTBotForecastScanner,
    MomentumCascadeLyroScanner.strategy_id: MomentumCascadeLyroScanner,
    LuxaraLivePlanQTMScanner.strategy_id: LuxaraLivePlanQTMScanner,
    LuxaraBreakBounceV27Scanner.strategy_id: LuxaraBreakBounceV27Scanner,
    VNEDGEAlgoMLProScanner.strategy_id: VNEDGEAlgoMLProScanner,
    ContextScalperV2.strategy_id: ContextScalperV2,
    QuantifiedFeeWallSniper.strategy_id: QuantifiedFeeWallSniper,
}


def get_strategy_class(strategy_id: str) -> type[BaseStrategy]:
    try:
        return STRATEGIES[strategy_id]
    except KeyError:
        raise KeyError(
            f"unknown strategy '{strategy_id}' — registered: {sorted(STRATEGIES)}"
        ) from None
