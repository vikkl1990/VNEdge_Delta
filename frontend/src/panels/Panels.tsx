// Core cockpit panels, wired to the live endpoints via TanStack Query. This is
// the growing parity subset (header, book, risk, positions, journal); the
// remaining classic panels port onto these same primitives incrementally.

import { DenseTable, TerminalBadge, TerminalPanel, type Column } from "../components/Terminal";
import { useJournal, useSnapshot, useWhoAmI } from "../queries";
import type { JournalRow, Position } from "../api";

const usd = (n: unknown) =>
  typeof n === "number" ? `${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}` : "—";
const signed = (n: unknown) => (typeof n === "number" && n < 0 ? "text-short" : "text-long");

export function Header() {
  const who = useWhoAmI();
  const snap = useSnapshot();
  const mode = (snap.data?.mode as string) ?? "…";
  const role = who.data?.role ?? "…";
  return (
    <header className="flex items-center justify-between gap-4 flex-wrap">
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-md border border-brand/40 grid place-items-center text-brand font-mono">
          VN
        </div>
        <div>
          <div className="text-[15px] font-semibold">VN Edge — Control Room</div>
          <div className="text-[11px] font-mono text-dim">React · TanStack · v2</div>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <TerminalBadge tone="info">mode {mode}</TerminalBadge>
        <TerminalBadge tone={snap.data?.kill_switch_active ? "bad" : "good"}>
          kill {snap.data?.kill_switch_active ? "ARMED" : "clear"}
        </TerminalBadge>
        <TerminalBadge tone="neutral">
          {who.data?.name ?? "…"} · {role}
        </TerminalBadge>
      </div>
    </header>
  );
}

function Kpi({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <div className="text-[11px] uppercase text-dim font-mono">{label}</div>
      <div className={`text-2xl font-mono tabular-nums ${tone ?? ""}`}>{value}</div>
    </div>
  );
}

export function BookPanel() {
  const { data, isLoading, isError } = useSnapshot();
  return (
    <TerminalPanel title="Book" meta={isLoading ? "loading…" : isError ? "error" : "live · 5s"}>
      <div className="flex items-end gap-10 flex-wrap">
        <Kpi label="Equity" value={usd(data?.equity)} />
        <Kpi label="Realized" value={usd(data?.realized_pnl)} tone={signed(data?.realized_pnl)} />
        <Kpi label="Unrealized" value={usd(data?.unrealized_pnl)} tone={signed(data?.unrealized_pnl)} />
        <Kpi label="Peak" value={usd(data?.peak_equity)} />
      </div>
    </TerminalPanel>
  );
}

export function RiskPanel() {
  const { data } = useSnapshot();
  const status = (data?.risk_status as string) ?? "—";
  const statusTone = status.toLowerCase().includes("ok") ? "good" : status === "—" ? "neutral" : "warn";
  return (
    <TerminalPanel title="Risk" meta="gateway · breaker · kill">
      <div className="flex items-center gap-3 flex-wrap">
        <TerminalBadge tone={statusTone as never}>{status}</TerminalBadge>
        <TerminalBadge tone={data?.live_trading_enabled ? "bad" : "good"}>
          live {data?.live_trading_enabled ? "ENABLED" : "locked"}
        </TerminalBadge>
      </div>
      <div className="flex items-end gap-10 flex-wrap mt-4">
        <Kpi label="Daily PnL" value={usd(data?.daily_pnl)} tone={signed(data?.daily_pnl)} />
        <Kpi
          label="Loss streak"
          value={typeof data?.consecutive_losses === "number" ? String(data.consecutive_losses) : "—"}
        />
        <Kpi label="Fills" value={typeof data?.fills === "number" ? String(data.fills) : "—"} />
        <Kpi label="Fees" value={usd(data?.fees_usd)} />
      </div>
    </TerminalPanel>
  );
}

export function PositionsPanel() {
  const { data } = useSnapshot();
  const rows = (data?.positions as Position[] | undefined) ?? [];
  const cols: Column<Position>[] = [
    { key: "sym", header: "Symbol", render: (r) => <span className="font-mono">{r.symbol ?? "—"}</span> },
    { key: "side", header: "Side", render: (r) => r.side ?? "—" },
    { key: "qty", header: "Qty", align: "right", render: (r) => (typeof r.quantity === "number" ? r.quantity : "—") },
    {
      key: "upnl",
      header: "uPnL",
      align: "right",
      render: (r) => <span className={signed(r.unrealized_pnl_usd)}>{usd(r.unrealized_pnl_usd)}</span>,
    },
  ];
  return (
    <TerminalPanel title="Positions" meta={`${rows.length} open`}>
      <DenseTable columns={cols} rows={rows} empty="flat — no open positions" />
    </TerminalPanel>
  );
}

const num = (n: unknown, d = 2) => (typeof n === "number" ? n.toFixed(d) : "—");

export function MarketPanel() {
  const { data } = useSnapshot();
  const p = data?.price ?? null;
  const fr = data?.funding_rate;
  return (
    <TerminalPanel title="Market" meta={(data?.symbol as string) ?? "—"}>
      {p ? (
        <div className="flex items-end gap-10 flex-wrap">
          <Kpi label="Mid" value={typeof p.mid === "number" ? p.mid.toLocaleString() : "—"} />
          <Kpi label="Bid" value={typeof p.bid === "number" ? p.bid.toLocaleString() : "—"} />
          <Kpi label="Ask" value={typeof p.ask === "number" ? p.ask.toLocaleString() : "—"} />
          <Kpi label="Spread" value={`${num(p.spread_bps, 1)} bps`} />
          <Kpi
            label="Funding"
            value={typeof fr === "number" ? `${(fr * 100).toFixed(4)}%` : "—"}
            tone={typeof fr === "number" ? signed(fr) : ""}
          />
        </div>
      ) : (
        <div className="text-[12px] font-mono text-dim">no live quote (warming / no book)</div>
      )}
    </TerminalPanel>
  );
}

export function FeedPanel() {
  const { data } = useSnapshot();
  const f = data?.feed_health ?? {};
  // Freshness is computed SERVER-side into these status strings (OK / stale) —
  // trust them rather than recomputing against a client clock and a field whose
  // epoch semantics vary by context.
  const tone = (v?: string) =>
    !v ? "neutral" : v.toLowerCase().includes("ok") ? "good" : "warn";
  const chip = (label: string, v?: string) => (
    <div className="flex items-center gap-2">
      <span className="text-[11px] uppercase text-dim font-mono">{label}</span>
      <TerminalBadge tone={tone(v) as never}>{v ?? "—"}</TerminalBadge>
    </div>
  );
  return (
    <TerminalPanel title="Feed health" meta={f.exchange ?? "—"}>
      <div className="flex items-center gap-6 flex-wrap">
        {chip("candles", f.candles)}
        {chip("funding", f.funding)}
        {chip("OI", f.open_interest)}
      </div>
    </TerminalPanel>
  );
}

export function JournalPanel() {
  const { data, isLoading } = useJournal(50);
  const rows = data ?? [];
  const cols: Column<JournalRow>[] = [
    { key: "lane", header: "Lane", render: (r) => <span className="font-mono">{r.lane ?? r.symbol ?? "—"}</span> },
    { key: "side", header: "Side", render: (r) => r.side ?? "—" },
    {
      key: "pnl",
      header: "Net PnL",
      align: "right",
      render: (r) => <span className={signed(r.net_pnl_usd)}>{usd(r.net_pnl_usd)}</span>,
    },
    { key: "exit", header: "Exit", render: (r) => <span className="text-dim">{r.exit_reason ?? "—"}</span> },
  ];
  return (
    <TerminalPanel title="Journal" meta={isLoading ? "loading…" : `${rows.length} rows · 20s`}>
      <DenseTable columns={cols} rows={rows} empty="no closed trades yet" />
    </TerminalPanel>
  );
}
