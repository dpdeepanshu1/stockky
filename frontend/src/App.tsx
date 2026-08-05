import { useState } from "react";
import { api, Decision, ScanResult } from "./api";
import Pipeline from "./components/Pipeline";
import DecisionCard from "./components/DecisionCard";
import ScanPanel from "./components/ScanPanel";

type ViewState =
  | { mode: "idle" }
  | { mode: "loading" }
  | { mode: "stock"; data: Decision }
  | { mode: "scan"; data: ScanResult }
  | { mode: "error"; message: string };

export default function App() {
  const [query, setQuery] = useState("");
  const [view, setView] = useState<ViewState>({ mode: "idle" });

  async function handleSearch(symbol: string) {
    if (!symbol.trim()) return;
    setView({ mode: "loading" });
    try {
      const data = await api.getStock(symbol.trim());
      setView({ mode: "stock", data });
    } catch (e) {
      setView({ mode: "error", message: (e as Error).message });
    }
  }

  async function handleScan() {
    setView({ mode: "loading" });
    try {
      const data = await api.runScan();
      setView({ mode: "scan", data });
    } catch (e) {
      setView({ mode: "error", message: (e as Error).message });
    }
  }

  return (
    <div className="min-h-screen bg-ink text-paper">
      <header className="border-b border-slate/60">
        <div className="max-w-5xl mx-auto px-6 py-6 flex items-center justify-between">
          <div>
            <div className="font-display text-2xl tracking-tight">Stockky</div>
            <div className="font-mono text-[11px] text-mist tracking-widest uppercase">
              AI Equity Research Analyst
            </div>
          </div>
          <div className="font-mono text-xs text-mist">
            NSE · India
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-12">
        {/* Hero: the one question this platform answers */}
        <section className="mb-12">
          <h1 className="font-display text-4xl md:text-5xl leading-tight max-w-2xl">
            What should I do <span className="italic text-mist">now?</span>
          </h1>
          <p className="text-mist mt-4 max-w-xl">
            Search a stock, or run the scanner across the watchlist. Every answer is one
            of five decisions — never a maybe.
          </p>

          <div className="flex flex-col sm:flex-row gap-3 mt-8">
            <div className="flex-1 flex items-center gap-2 border border-slate rounded-lg px-4 py-3 bg-graphite focus-within:border-signal-prepare/60 transition">
              <span className="font-mono text-mist text-sm">NSE:</span>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSearch(query)}
                placeholder="TCS, INFY, RELIANCE..."
                className="bg-transparent outline-none flex-1 font-mono text-sm placeholder:text-mist/40"
              />
              <button
                onClick={() => handleSearch(query)}
                className="text-xs font-mono uppercase tracking-wide text-signal-prepare hover:text-paper transition"
              >
                Analyze
              </button>
            </div>
            <button
              onClick={handleScan}
              className="border border-slate rounded-lg px-6 py-3 font-mono text-xs uppercase tracking-widest text-mist hover:text-paper hover:border-mist transition whitespace-nowrap"
            >
              Run market scan
            </button>
          </div>
        </section>

        {/* Result area */}
        <section>
          {view.mode === "idle" && (
            <div className="text-mist/50 text-sm font-mono text-center py-16 border border-dashed border-slate rounded-xl">
              Search a symbol or run the scanner to begin.
            </div>
          )}

          {view.mode === "loading" && (
            <div className="rounded-xl border border-slate bg-graphite p-8">
              <Pipeline running={true} />
            </div>
          )}

          {view.mode === "error" && (
            <div className="rounded-xl border border-signal-sell/40 bg-signal-sell/10 p-6 text-signal-sell text-sm font-mono">
              {view.message}
            </div>
          )}

          {view.mode === "stock" && <DecisionCard data={view.data} />}

          {view.mode === "scan" && (
            <ScanPanel result={view.data} onSelect={(s) => handleSearch(s)} />
          )}
        </section>
      </main>

      <footer className="max-w-5xl mx-auto px-6 py-8 border-t border-slate/60 mt-12">
        <p className="text-xs text-mist/50 font-mono">
          Informational only, not investment advice. Verify before you trade.
        </p>
      </footer>
    </div>
  );
}
