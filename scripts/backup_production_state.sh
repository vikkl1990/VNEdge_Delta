#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
destination="${1:-$repo_root/backups}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$destination/vnedge-state-$timestamp.tar.gz"
mkdir -p "$destination"

if command -v docker >/dev/null 2>&1; then
  running="$(cd "$repo_root" && docker compose ps --status running --services 2>/dev/null || true)"
  if printf '%s\n' "$running" | grep -qx 'delta-live-small'; then
    printf '%s\n' 'Refusing backup while delta-live-small is running; stop and reconcile it first.' >&2
    exit 2
  fi
fi

tar -C "$repo_root" -czf "$archive" \
  --exclude='*.tmp' \
  data logs research/paper_trials research/live_research
shasum -a 256 "$archive" > "$archive.sha256"
chmod 600 "$archive" "$archive.sha256"
printf '%s\n' "$archive"
