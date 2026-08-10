"""Meta-labeling harness: train P(rule-signal wins after costs), validate it
through the locked anti-overfit gates, and emit a gated verdict.

Orchestration ONLY — it composes the existing, tested primitives
(build_meta_label_dataset, train_classifier, ProbabilityCalibrator, the CPCV /
deflated-Sharpe / PBO validators). It never trades and never promotes: it
produces a candidate report. A model only trades via MLStrategy behind the
gateway, and only after pre-registered untouched-data judgment — the gates here
are a pre-filter, not the promotion.

Baseline to beat: take EVERY signal. The meta-labeler earns its place only if
filtering to high-confidence signals improves net P&L OUT-OF-SAMPLE. Below the
label threshold the harness reports COLLECTING_LABELS rather than fitting noise
(the trainer itself refuses < 200 rows). None of the gate thresholds may be
tuned to make a result pass — that is the overfitting trap this exists to stop.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from vnedge.ml.feature_matrix import FEATURE_COLUMNS
from vnedge.ml.robustness import expected_calibration_error
from vnedge.ml.trainer import train_classifier
from vnedge.ml.validation import (
    combinatorial_purged_splits,
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
)

_MIN_TRAIN_ROWS = 200  # the trainer's hard floor — every CPCV train fold needs this


@dataclass(frozen=True)
class MetaLabelGates:
    """The LOCKED promotion pre-filter. Do NOT tune these to pass a result."""

    min_labels: int = 200            # below this: keep collecting, do not fit
    cpcv_min_labels: int = 300       # below this: single-fit only, no OOS gates
    n_groups: int = 6
    n_test_groups: int = 2
    embargo_pct: float = 0.02
    prob_threshold: float = 0.50
    # Deliberately WIDE so the configs select genuinely different trade sets —
    # near-identical thresholds make PBO measure noise, not overfit.
    threshold_grid: tuple[float, ...] = (0.30, 0.40, 0.50, 0.60, 0.70)
    min_cpcv_median_pf: float = 1.30
    min_deflated_sharpe: float = 0.95
    max_pbo: float = 0.20


@dataclass(frozen=True)
class GateCheck:
    name: str
    value: float | None
    threshold: float
    direction: str  # ">=" or "<="
    ok: bool


@dataclass(frozen=True)
class MetaLabelReport:
    status: str
    samples: int
    win_rate: float
    trainable: bool
    passed: bool
    beats_baseline: bool | None
    metrics: dict = field(default_factory=dict)
    gates: list[GateCheck] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # invariants surfaced for the dashboard / any consumer
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        out = asdict(self)
        out["gates"] = [asdict(g) for g in self.gates]
        return out


def _profit_factor(returns: np.ndarray) -> float | None:
    r = np.asarray(returns, dtype=float)
    if r.size == 0:
        return None
    wins = float(r[r > 0].sum())
    losses = float(-r[r < 0].sum())
    if losses <= 0.0:
        return None  # undefined (no losers) — never inf
    return wins / losses


def _sharpe(returns: np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return 0.0
    sd = float(r.std(ddof=1))
    return float(r.mean() / sd) if sd > 0.0 else 0.0


def _even_blocks(n: int, cap: int = 16) -> int:
    """Largest even block count <= min(cap, n) for PBO (needs even, >= 2)."""
    b = min(cap, n)
    if b < 2:
        return 0
    return b if b % 2 == 0 else b - 1


def evaluate_meta_labeler(
    frame: pd.DataFrame, *, gates: MetaLabelGates = MetaLabelGates()
) -> MetaLabelReport:
    """Train + validate the meta-labeler on a built dataset frame.

    ``frame`` is the output of build_meta_label_dataset: FEATURE_COLUMNS +
    ``meta_label`` (1 win / 0 loss) + ``net_usd`` per closed trade.
    """
    samples = int(len(frame))
    win_rate = float(frame["meta_label"].mean()) if samples else 0.0

    if samples < gates.min_labels:
        return MetaLabelReport(
            status="COLLECTING_LABELS", samples=samples, win_rate=win_rate,
            trainable=False, passed=False, beats_baseline=None,
            metrics={"min_labels": gates.min_labels,
                     "progress_pct": round(100.0 * samples / gates.min_labels, 1)},
            reasons=[f"{samples}/{gates.min_labels} labels — accumulate more closed "
                     f"trades before fitting (the trainer refuses < 200 rows)"],
        )

    X = frame[list(FEATURE_COLUMNS)].astype(float)
    y = frame["meta_label"].astype(float)
    net = frame["net_usd"].astype(float).to_numpy()

    if y.nunique() < 2:
        return MetaLabelReport(
            status="SINGLE_CLASS", samples=samples, win_rate=win_rate,
            trainable=False, passed=False, beats_baseline=None,
            reasons=["labels are single-class (all wins or all losses) — nothing to learn yet"],
        )

    if samples < gates.cpcv_min_labels:
        # enough to fit one model, not enough for leakage-safe OOS gates
        train_classifier(X, y, compute_importances=False)  # proves it fits; discard
        return MetaLabelReport(
            status="TRAINABLE_INSUFFICIENT_FOR_CPCV", samples=samples, win_rate=win_rate,
            trainable=True, passed=False, beats_baseline=None,
            metrics={"cpcv_min_labels": gates.cpcv_min_labels},
            reasons=[f"{samples} labels — enough to fit, need >= {gates.cpcv_min_labels} "
                     f"so every CPCV train fold clears the 200-row floor"],
        )

    # --- full combinatorial purged CV -----------------------------------------
    splits = combinatorial_purged_splits(
        samples, n_groups=gates.n_groups, n_test_groups=gates.n_test_groups,
        embargo_pct=gates.embargo_pct,
    )
    oos_pred_sum = np.zeros(samples)
    oos_pred_cnt = np.zeros(samples)
    fold_pfs: list[float] = []
    fold_meta_net: list[float] = []
    fold_base_net: list[float] = []
    trainable_folds = 0
    for train_idx, test_idx in splits:
        if len(train_idx) < _MIN_TRAIN_ROWS or y.iloc[train_idx].nunique() < 2:
            continue
        trainable_folds += 1
        model = train_classifier(X.iloc[train_idx], y.iloc[train_idx], compute_importances=False)
        p = model.predict_proba_up(X.iloc[test_idx])
        oos_pred_sum[test_idx] += p
        oos_pred_cnt[test_idx] += 1
        sel = p >= gates.prob_threshold
        test_net = net[test_idx]
        fold_meta_net.append(float(test_net[sel].sum()))
        fold_base_net.append(float(test_net.sum()))
        pf = _profit_factor(test_net[sel])
        if pf is not None:
            fold_pfs.append(pf)

    if trainable_folds < 2:
        return MetaLabelReport(
            status="INSUFFICIENT_FOLDS", samples=samples, win_rate=win_rate,
            trainable=True, passed=False, beats_baseline=None,
            metrics={"trainable_folds": trainable_folds},
            reasons=["too few CPCV folds cleared the 200-row train floor — need more labels"],
        )

    mask = oos_pred_cnt > 0
    oos_p = oos_pred_sum[mask] / oos_pred_cnt[mask]
    oos_net = net[mask]
    oos_y = y.to_numpy()[mask]
    # Order OOS observations by entry time so PBO's in-sample/out-of-sample
    # block split reflects TEMPORAL generalization, not row order.
    if "entry_ts" in frame.columns:
        order = np.argsort(frame["entry_ts"].to_numpy()[mask], kind="stable")
        oos_p, oos_net, oos_y = oos_p[order], oos_net[order], oos_y[order]

    sel = oos_p >= gates.prob_threshold
    meta_returns = oos_net[sel]
    meta_net = float(meta_returns.sum())
    base_net = float(oos_net.sum())
    beats_baseline = bool(meta_net > base_net)
    median_pf = float(np.median(fold_pfs)) if fold_pfs else None
    # Deflate the chosen threshold's Sharpe by the breadth of the threshold
    # search (its variance across all thresholds tried) — this is what stops a
    # lucky threshold from looking real.
    trial_sharpes = [_sharpe(oos_net[oos_p >= thr]) for thr in gates.threshold_grid]
    dsr = (
        float(deflated_sharpe_ratio(
            meta_returns, n_trials=len(gates.threshold_grid), trial_sharpes=trial_sharpes))
        if meta_returns.size > 2 and np.asarray(trial_sharpes).std() > 0 else None
    )
    perf = np.column_stack([np.where(oos_p >= thr, oos_net, 0.0) for thr in gates.threshold_grid])
    n_blocks = _even_blocks(len(oos_net))
    pbo = (
        float(probability_of_backtest_overfitting(perf, n_blocks=n_blocks))
        if n_blocks >= 2 else None
    )
    ece = float(expected_calibration_error(oos_y, oos_p))

    checks = [
        GateCheck("cpcv_median_pf", median_pf, gates.min_cpcv_median_pf, ">=",
                  median_pf is not None and median_pf >= gates.min_cpcv_median_pf),
        GateCheck("deflated_sharpe", dsr, gates.min_deflated_sharpe, ">=",
                  dsr is not None and dsr >= gates.min_deflated_sharpe),
        GateCheck("pbo", pbo, gates.max_pbo, "<=",
                  pbo is not None and pbo <= gates.max_pbo),
        GateCheck("beats_baseline", 1.0 if beats_baseline else 0.0, 1.0, ">=", beats_baseline),
    ]
    passed = all(c.ok for c in checks)
    reasons = [f"{c.name} {c.value if c.value is not None else '—'} {c.direction} "
               f"{c.threshold}: {'PASS' if c.ok else 'FAIL'}" for c in checks]

    return MetaLabelReport(
        status="VALIDATED_PASS" if passed else "VALIDATED_FAIL",
        samples=samples, win_rate=win_rate, trainable=True, passed=passed,
        beats_baseline=beats_baseline,
        metrics={
            "cpcv_folds": trainable_folds,
            "oos_trades": int(mask.sum()),
            "meta_net_usd": round(meta_net, 4),
            "baseline_net_usd": round(base_net, 4),
            "meta_selected": int(sel.sum()),
            "cpcv_median_pf": median_pf,
            "deflated_sharpe": dsr,
            "pbo": pbo,
            "ece": ece,
            "prob_threshold": gates.prob_threshold,
        },
        gates=checks, reasons=reasons,
    )
