// Terminal* component library — the small, composable primitives the roadmap
// called for (TerminalPanel, DenseTable, TerminalBadge, TerminalTabs) so panels
// compose instead of living in one monolithic HTML file.

import type { ReactNode } from "react";

export function TerminalPanel({
  title,
  meta,
  children,
}: {
  title: string;
  meta?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-xl border border-line bg-panel/70 overflow-hidden">
      <header className="flex items-center justify-between px-4 py-3 border-b border-line">
        <h2 className="text-[13px] font-semibold tracking-wide text-txt">{title}</h2>
        {meta ? <span className="text-[11px] font-mono text-dim">{meta}</span> : null}
      </header>
      <div className="p-4">{children}</div>
    </section>
  );
}

type Tone = "neutral" | "good" | "warn" | "bad" | "info";
const toneClass: Record<Tone, string> = {
  neutral: "text-dim border-line",
  good: "text-long border-long/40",
  warn: "text-warn border-warn/40",
  bad: "text-short border-short/40",
  info: "text-info border-info/40",
};

export function TerminalBadge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-md border px-2 py-[2px] text-[11px] font-mono uppercase ${toneClass[tone]}`}
    >
      {children}
    </span>
  );
}

export interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  align?: "left" | "right";
}

export function DenseTable<T>({
  columns,
  rows,
  empty = "no rows",
}: {
  columns: Column<T>[];
  rows: T[];
  empty?: string;
}) {
  if (!rows.length) return <div className="text-[12px] font-mono text-dim py-2">{empty}</div>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr className="text-faint uppercase text-[10px]">
            {columns.map((c) => (
              <th
                key={c.key}
                className={`font-mono font-normal py-1.5 px-2 ${c.align === "right" ? "text-right" : "text-left"}`}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-t border-line/60">
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={`py-1.5 px-2 tabular-nums ${c.align === "right" ? "text-right" : "text-left"}`}
                >
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function TerminalTabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: string; label: string }[];
  active: string;
  onChange: (id: string) => void;
}) {
  return (
    <nav className="flex gap-1 flex-wrap">
      {tabs.map((t) => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={`rounded-md px-3 py-1.5 text-[12px] font-mono border transition-colors ${
            active === t.id
              ? "border-brand/50 text-brand bg-brand/10"
              : "border-transparent text-dim hover:text-txt"
          }`}
        >
          {t.label}
        </button>
      ))}
    </nav>
  );
}
