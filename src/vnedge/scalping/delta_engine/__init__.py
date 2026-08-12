"""Delta India multi-timeframe scalper research engine.

The package is deliberately execution-neutral.  It creates causal signal
plans, journals every decision, and can adapt a selected plan into the
existing risk gateway.  It never submits an order itself.
"""

from vnedge.scalping.delta_engine.absorption import (
    AbsorptionDetector,
    AbsorptionDetectorConfig,
    AbsorptionInstrumentConfig,
    AbsorptionObservation,
    FootprintLevel,
    LiquidationCluster,
)
from vnedge.scalping.delta_engine.absorption_research import (
    AbsorptionResearchConfig,
    AbsorptionResearchOutcome,
    AbsorptionResearchSummary,
    AbsorptionResearchTracker,
    summarize_absorption_outcomes,
)
from vnedge.scalping.delta_engine.architecture import architecture_manifest
from vnedge.scalping.delta_engine.candle_store import MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.context import MarketContextBuilder
from vnedge.scalping.delta_engine.event_trigger import (
    AbsorptionReversalScanner,
    DeltaVerifiedEventBridge,
    EventDrivenTriggerLayer,
    EventMarketSnapshot,
    EventTriggerConfig,
    EventTriggerDecision,
    SustainedFlowImbalanceScanner,
)
from vnedge.scalping.delta_engine.factory import (
    DeltaScalperAssembly,
    build_delta_scalper_assembly,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel, FeeBreakdown
from vnedge.scalping.delta_engine.flow_store import (
    ChannelSequenceTracker,
    FlowSnapshot,
    L2TradeFlowStore,
    SequenceHealth,
)
from vnedge.scalping.delta_engine.forward_tracker import (
    ForwardOutcome,
    ForwardOutcomeTracker,
)
from vnedge.scalping.delta_engine.indicator_scoring import (
    CandidateEconomics,
    FamilyPolicy,
    IndicatorEvidence,
    IndicatorFamily,
    IndicatorFamilyScore,
    IndicatorFamilyScorer,
    IndicatorScoreResult,
    IndicatorScoringConfig,
    candle_indicator_evidence,
    default_indicator_scoring_config,
    event_indicator_evidence,
    historical_trade_indicator_evidence,
    load_indicator_scoring_config,
)
from vnedge.scalping.delta_engine.scanners import (
    MomentumBurstScanner,
    OrderFlowImbalanceFadeScanner,
    Scanner,
)
from vnedge.scalping.delta_engine.signal_generator import (
    DeltaScalperSignalGenerator,
    EngineDecision,
    PipelineStage,
    ScalperRiskAdapter,
)
from vnedge.scalping.delta_engine.types import (
    Candle,
    ExitPath,
    L2Confirmation,
    MarketContext,
    Regime,
    Side,
    SignalCandidate,
)
from vnedge.scalping.delta_engine.validation import (
    RobustValidationReport,
    fee_sensitivity,
    robust_validation_report,
    untouched_window_summary,
)

__all__ = [
    "AbsorptionDetector",
    "AbsorptionDetectorConfig",
    "AbsorptionInstrumentConfig",
    "AbsorptionObservation",
    "AbsorptionResearchConfig",
    "AbsorptionResearchOutcome",
    "AbsorptionResearchSummary",
    "AbsorptionResearchTracker",
    "AbsorptionReversalScanner",
    "CandidateEconomics",
    "Candle",
    "ChannelSequenceTracker",
    "DeltaFeeModel",
    "DeltaScalperAssembly",
    "DeltaScalperSignalGenerator",
    "DeltaVerifiedEventBridge",
    "EngineDecision",
    "EventDrivenTriggerLayer",
    "EventMarketSnapshot",
    "EventTriggerConfig",
    "EventTriggerDecision",
    "ExitPath",
    "FamilyPolicy",
    "FeeBreakdown",
    "FlowSnapshot",
    "FootprintLevel",
    "ForwardOutcome",
    "ForwardOutcomeTracker",
    "IndicatorEvidence",
    "IndicatorFamily",
    "IndicatorFamilyScore",
    "IndicatorFamilyScorer",
    "IndicatorScoreResult",
    "IndicatorScoringConfig",
    "L2Confirmation",
    "L2TradeFlowStore",
    "LiquidationCluster",
    "MarketContext",
    "MarketContextBuilder",
    "MomentumBurstScanner",
    "MultiTimeframeCandleStore",
    "OrderFlowImbalanceFadeScanner",
    "PipelineStage",
    "Regime",
    "RobustValidationReport",
    "ScalperRiskAdapter",
    "Scanner",
    "SequenceHealth",
    "Side",
    "SignalCandidate",
    "SustainedFlowImbalanceScanner",
    "architecture_manifest",
    "build_delta_scalper_assembly",
    "candle_indicator_evidence",
    "default_indicator_scoring_config",
    "event_indicator_evidence",
    "fee_sensitivity",
    "historical_trade_indicator_evidence",
    "load_indicator_scoring_config",
    "robust_validation_report",
    "summarize_absorption_outcomes",
    "untouched_window_summary",
]
