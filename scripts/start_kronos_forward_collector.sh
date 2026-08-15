#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export PYTHONPATH="${PYTHONPATH:-src}"
export HF_HUB_OFFLINE=1

exec "$REPO_ROOT/.venv/bin/python" -u -m vnedge.research.kronos_forward_collector \
  --repo-root "$REPO_ROOT" \
  --kronos-repo "$REPO_ROOT/models/kronos/upstream" \
  --registry "$REPO_ROOT/configs/strategy_registry.yaml" \
  --interval-seconds 60
