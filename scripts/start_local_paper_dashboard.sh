#!/usr/bin/env bash
set -euo pipefail

# Local VNEDGE paper runtime. This launches the real simulated-fill pipeline:
# live public market data -> strategy -> risk gateway -> paper broker -> journal.
# The multi-lane runtime has no live execution adapter and cannot send orders.

: "${DASHBOARD_TOKEN:?Set DASHBOARD_TOKEN before starting the local paper dashboard}"

export PYTHONPATH="${PYTHONPATH:-src}"
export DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
export DASHBOARD_PORT="${DASHBOARD_PORT:-8080}"

# Explicit paper/shadow boundary. Keep failed research families pruned and do
# not manufacture fills by mirroring every experimental shadow lane.
export MULTI_LANE_MODES="${MULTI_LANE_MODES:-paper,shadow}"
export MULTI_LANE_EXCHANGES="${MULTI_LANE_EXCHANGES:-binanceusdm,bybit,delta_india}"
export MULTI_LANE_PRUNE_DEAD="${MULTI_LANE_PRUNE_DEAD:-1}"
export MULTI_LANE_PAPER_OBSERVE_ALL="${MULTI_LANE_PAPER_OBSERVE_ALL:-0}"
export MULTI_LANE_CRYPTO_TREND_DOGE="${MULTI_LANE_CRYPTO_TREND_DOGE:-1}"
export MULTI_LANE_CRYPTO_TREND_DOGE_PAPER="${MULTI_LANE_CRYPTO_TREND_DOGE_PAPER:-1}"
export MULTI_LANE_EVIDENCE_PAPER_TRIAL="${MULTI_LANE_EVIDENCE_PAPER_TRIAL:-1}"
export MULTI_LANE_MANIFEST_RELOAD="${MULTI_LANE_MANIFEST_RELOAD:-1}"
export MULTI_LANE_JOURNAL_DIR="${MULTI_LANE_JOURNAL_DIR:-logs/paper_trials}"

python_bin="${VNEDGE_PYTHON:-.venv/bin/python}"
exec "$python_bin" -m vnedge.runtime.multi_lane_shadow
