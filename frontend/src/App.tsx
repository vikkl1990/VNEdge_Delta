import { useMemo, useState } from "react";
import { CommandPalette, type Command } from "./components/CommandPalette";
import { TerminalTabs } from "./components/Terminal";
import {
  BookPanel,
  FeedPanel,
  Header,
  JournalPanel,
  MarketPanel,
  PositionsPanel,
  RiskPanel,
} from "./panels/Panels";
import { useUi } from "./store";

const TABS = [
  { id: "desk", label: "Desk" },
  { id: "markets", label: "Markets" },
  { id: "journal", label: "Journal" },
];

export default function App() {
  const [tab, setTab] = useState("desk");
  const setPalette = useUi((s) => s.setPalette);

  const commands: Command[] = useMemo(
    () => [
      { id: "desk", label: "Desk", hint: "book · risk · positions", run: () => setTab("desk") },
      { id: "markets", label: "Markets", hint: "price · spread · feed", run: () => setTab("markets") },
      { id: "journal", label: "Journal", hint: "closed trades", run: () => setTab("journal") },
      {
        id: "classic",
        label: "Classic dashboard ↗",
        hint: "the full v1 cockpit",
        run: () => {
          const t = new URLSearchParams(window.location.search).get("token");
          window.location.href = t ? `/?token=${encodeURIComponent(t)}` : "/";
        },
      },
    ],
    [setTab],
  );

  return (
    <div className="min-h-full max-w-[1180px] mx-auto px-5 py-6 flex flex-col gap-5">
      <Header />
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <TerminalTabs tabs={TABS} active={tab} onChange={setTab} />
        <button
          onClick={() => setPalette(true)}
          className="rounded-md border border-line px-3 py-1.5 text-[12px] font-mono text-dim hover:text-txt"
        >
          ⌘/Ctrl-K
        </button>
      </div>

      {tab === "desk" && (
        <div className="flex flex-col gap-5">
          <BookPanel />
          <RiskPanel />
          <PositionsPanel />
        </div>
      )}
      {tab === "markets" && (
        <div className="flex flex-col gap-5">
          <MarketPanel />
          <FeedPanel />
        </div>
      )}
      {tab === "journal" && <JournalPanel />}

      <footer className="text-[11px] font-mono text-faint pt-2">
        v2 · read-only · classic dashboard remains at <code>/</code>
      </footer>

      <CommandPalette commands={commands} />
    </div>
  );
}
