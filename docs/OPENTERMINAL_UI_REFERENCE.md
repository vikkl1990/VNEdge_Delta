# OpenTerminalUI-Inspired Cockpit Pass

Date: 2026-07-31

Reference reviewed: Hitheshkaranth/OpenTerminalUI.

## What VNEDGE Adapted

- Persistent workspace rail for fast operator context switching.
- GO-style command bar for view routing and future symbol/lane lookup.
- Market tape strip fed from the existing lane snapshot instead of demo prices.
- Always-visible provenance and safety pills: gateway, live lock, build hash.
- Dense terminal framing around existing operator data rather than a marketing layout.

## What VNEDGE Did Not Copy

- No OpenTerminalUI source code was copied into VNEDGE.
- No new execution controls were added from the UI.
- No synthetic market or order data was introduced.
- Existing cockpit panels and safety/readiness instruments stay intact.

## Why This Shape

VNEDGE is not a general financial terminal. It is a crypto execution assistant
with strict promotion, risk, journal, and evidence gates. The UI should feel like
a production trading workstation, but it must remain read-only and grounded in
the bot's real state.

This pass therefore upgrades the shell and navigation while preserving the
operator surfaces that already matter: lane health, signal funnel, journal,
positions, promotion readiness, model/research evidence, and system status.
