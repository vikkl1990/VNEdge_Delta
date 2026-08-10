// Ctrl/Cmd-K command palette — React port of the classic dashboard's palette,
// over a command registry. Fuzzy subsequence filter, keyboard-navigable.

import { useEffect, useMemo, useRef, useState } from "react";
import { useUi } from "../store";

export interface Command {
  id: string;
  label: string;
  hint: string;
  run: () => void;
}

function score(hay: string, q: string): number {
  let qi = 0,
    s = 0,
    last = -1;
  const h = hay.toLowerCase();
  for (let i = 0; i < h.length && qi < q.length; i++) {
    if (h[i] === q[qi]) {
      s += last >= 0 && i === last + 1 ? 2 : 1;
      last = i;
      qi++;
    }
  }
  return qi === q.length ? s : -1;
}

export function CommandPalette({ commands }: { commands: Command[] }) {
  const open = useUi((s) => s.paletteOpen);
  const setOpen = useUi((s) => s.setPalette);
  const toggle = useUi((s) => s.togglePalette);
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        toggle();
      } else if (e.key === "Escape") {
        setOpen(false);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [toggle, setOpen]);

  useEffect(() => {
    if (open) {
      setQ("");
      setActive(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  const filtered = useMemo(() => {
    const query = q.toLowerCase().replace(/\s+/g, "");
    if (!query) return commands;
    return commands
      .map((c) => ({ c, s: score(`${c.label} ${c.hint}`, query) }))
      .filter((x) => x.s >= 0)
      .sort((a, b) => b.s - a.s)
      .map((x) => x.c);
  }, [q, commands]);

  if (!open) return null;

  const run = (i: number) => {
    const cmd = filtered[i];
    if (!cmd) return;
    setOpen(false);
    cmd.run();
  };

  return (
    <div
      className="fixed inset-0 z-[9999] flex items-start justify-center pt-[12vh] bg-black/60"
      onClick={(e) => {
        if (e.target === e.currentTarget) setOpen(false);
      }}
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
    >
      <div className="w-[min(620px,92vw)] rounded-xl border border-brand/40 bg-panel overflow-hidden shadow-2xl">
        <input
          ref={inputRef}
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setActive(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActive((a) => Math.min(a + 1, filtered.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((a) => Math.max(a - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              run(active);
            }
          }}
          placeholder="Jump to a view or command…  (Esc to close)"
          className="w-full bg-transparent outline-none text-txt font-mono text-[15px] px-[18px] py-4 border-b border-line placeholder:text-faint"
          aria-label="Command palette search"
        />
        <ul className="max-h-[46vh] overflow-auto p-1.5 m-0 list-none">
          {filtered.length === 0 ? (
            <li className="px-4 py-3 text-dim font-mono text-[12px]">no matching command</li>
          ) : (
            filtered.map((c, i) => (
              <li
                key={c.id}
                onClick={() => run(i)}
                className={`flex items-center justify-between gap-3 rounded-md px-3 py-2 cursor-pointer ${
                  i === active ? "bg-brand/10" : "hover:bg-brand/5"
                }`}
              >
                <span className="text-[13px]">{c.label}</span>
                <span className={`font-mono text-[11px] ${i === active ? "text-brand" : "text-dim"}`}>
                  {c.hint}
                </span>
              </li>
            ))
          )}
        </ul>
        <div className="flex gap-3.5 px-3.5 py-2 border-t border-line text-faint font-mono text-[11px]">
          <span>↑↓ navigate</span>
          <span>⏎ run</span>
          <span>⌘/Ctrl-K palette</span>
        </div>
      </div>
    </div>
  );
}
