# VN Edge UI (v2) — React foundation

A React / Vite / TypeScript / Tailwind rebuild of the dashboard, using
**TanStack Query** over the existing token-gated endpoints (`/state`,
`/journal`, `/whoami`). Zustand holds UI chrome only; all server state lives in
TanStack Query. Ships a small **Terminal\*** component library (`TerminalPanel`,
`DenseTable`, `TerminalBadge`, `TerminalTabs`) and a **Ctrl/Cmd-K** command
palette.

## Status
**Foundation + core panels (Desk book, Journal), not full parity.** The classic
dashboard remains the primary cockpit at `/`; this ships alongside it at `/app`.
Remaining classic panels port onto these same primitives incrementally.

## Build & serve
```bash
npm --prefix frontend install
npm --prefix frontend run build      # -> frontend/dist
```
`create_app` mounts `frontend/dist` at **`/app`** *only when the build exists*
— so a production image without the build simply has no `/app` route (never a
500), and `/` is unaffected. Open `/app/?token=<DASHBOARD_TOKEN>`.

## Production (follow-up, not in this change)
`frontend/` is intentionally **not** in the Docker image inputs, so the running
image is unchanged and `/app` is absent in prod until a Node build stage is
added to the Dockerfile (must be built + verified on the VM, where Docker runs —
the dev Mac has no Docker).
