"""Scoped AI-agent gateway for VNEDGE.

The gateway is a separate surface from the human dashboard. It is mounted only
when explicit agent tokens are configured, and the first slice is research-only:
agents can read state/research artifacts and queue backtest requests, but they
cannot place orders or promote lanes.
"""

