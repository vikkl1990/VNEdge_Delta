"""Download CLI date-range resolution."""

import pytest

from vnedge.data.download import parse_args, resolve_range


def test_since_until_explicit():
    args = parse_args(["--since", "2024-07-03", "--until", "2025-07-03"])
    since_ms, until_ms = resolve_range(args)
    assert until_ms - since_ms == 365 * 86_400_000
    assert since_ms == 1_719_964_800_000  # 2024-07-03T00:00Z


def test_days_lookback_from_until():
    args = parse_args(["--until", "2025-07-03", "--days", "30"])
    since_ms, until_ms = resolve_range(args)
    assert until_ms - since_ms == 30 * 86_400_000


def test_empty_range_rejected():
    args = parse_args(["--since", "2025-07-03", "--until", "2024-07-03"])
    with pytest.raises(ValueError, match="empty range"):
        resolve_range(args)
