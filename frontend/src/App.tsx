import { useState, useEffect } from "react";
import { api, Decision, ScanResult } from "./api";
import Pipeline from "./components/Pipeline";
import DecisionCard from "./components/DecisionCard";
import ScanPanel from "./components/ScanPanel";
import WatchlistManager from "./components/WatchlistManager";

type ViewState =
  | { mode: "idle" }
  | { mode: "loading"; label: string }
  | { mode: "stock"; data: Decision }
  | { mode: "scan"; data: ScanResult }
  | { mode: "error"; message: string };

export default function App() {
  const [query, setQuery] = useState("");
  const [view, setView] = useState<ViewState>({ mode: "idle" });
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [showWatchlist, setShowWatchlist] = useState(false);

  useEffect(() => {
    api.getWatchlist().then((r) => setWatchlist(r.symbols)).catch(() => {});
  }, []);

  async function handleSearch(symbol: string) {
    if (!symbol.trim()) return;
    setView({ mode: "loading", label: `Analysing ${symbol.toUpperCase()}…` });
    try {
      const data = await api.getStock(symbol.trim());
      setView({ mode: "stock", data });
      setQuery("");
    } catch (e) {
      setView({ mode: "error", message: (e as Error).message });
    }
  }

  async function handleScan() {
    setView({ mode: "loading", label: "Running market scan…" });
    try {
      const data = await api.runScan();
      setView({ mode: "scan", data });
    } catch (e) {
      setView({ mode: "error", message: (e as Error).message });
    }
  }

  async function handleWatchlistUpdate(symbols: string[]) {
    await api.setWatchlist(symbols);
    setWatchlist(symbols);
  }

  return (
    <div className="min-h-screen bg-ink text-paper">
      {/* Nav */}
      <header className="sticky top-0 z-40 border-b border-slate/60 backdrop-blur-sm bg-ink/90">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div>
              <span className="font-display text-xl tracking-tight">Stockky</span>
              <span className="font-mono text-[10px] text-mist tracking-widest uppercase ml-3">NSE · India</span>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={() => setShowWatchlist(!showWatchlist)}
              className="flex items-center gap-2 text-xs font-mono text-mist hover:text-paper border border-slate rounded-lg px-3 py-2 hover:border-mist/60 transition"
            >
              <span className="w-1.5 h-1.5 rounded-full bg-signal-prepare inline-block" />
              Watchlist ({watchlist.length})
            </button>
          </div>
        </div>
      </header>

      {/* Watchlist drawer */}
      {showWatchlist && (
        <div className="border-b border-slate/60 bg-graphite">
          <div className="max-w-6xl mx-auto px-6 py-6">
            <WatchlistManager
              symbols={watchlist}
              onChange={handleWatchlistUpdate}
              onAnalyse={(s) => { setShowWatchlist(false); handleSearch(s); }}
            />
          </div>
        </div>
      )}

      <main className="max-w-6xl mx-auto px-6 py-10">
        {/* Hero */}
        <section className="mb-10">
          <h1 className="font-display text-4xl md:text-[52px] leading-tight max-w-2xl mb-2">
            What should I do<br />
            <span className="italic text-mist">with this stock?</span>
          </h1>
          <p className="text-mist text-sm max-w-lg mb-8">
            Technical + fundamental + news + AI — synthesised into one of five decisions. Never a maybe.
          </p>

          <div className="flex flex-col sm:flex-row gap-3">
            <div className="flex-1 flex items-center gap-2 border border-slate rounded-xl px-4 py-3.5 bg-graphite focus-within:border-signal-prepare/60 transition">
              <span className="font-mono text-mist text-xs select-none">NSE:</span>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value.toUpperCase())}
                onKeyDown={(e) => e.key === "Enter" && handleSearch(query)}
                placeholder="TCS, INFY, RELIANCE…"
                className="bg-transparent outline-none flex-1 font-mono text-sm placeholder:text-mist/30"
                autoComplete="off"
                spellCheck={false}
              />
              {query && (
                <button
                  onClick={() => handleSearch(query)}
                  className="text-[10px] font-mono uppercase tracking-widest bg-signal-prepare/10 text-signal-prepare border border-signal-prepare/30 rounded px-3 py-1.5 hover:bg-signal-prepare/20 transition"
                >
                  Analyse
                </button>
              )}
            </div>
            <button
              onClick={handleScan}
              className="border border-slate rounded-xl px-6 py-3.5 font-mono text-xs uppercase tracking-widest text-mist hover:text-paper hover:border-mist transition whitespace-nowrap bg-graphite"
            >
              Run market scan
            </button>
          </div>

          {/* Quick watchlist chips */}
          {watchlist.length > 0 && view.mode === "idle" && (
            <div className="flex flex-wrap gap-2 mt-4">
              {watchlist.slice(0, 10).map((s) => (
                <button
                  key={s}
                  onClick={() => handleSearch(s)}
                  className="font-mono text-[11px] text-mist hover:text-paper border border-slate/60 hover:border-mist/60 rounded-md px-2.5 py-1 transition"
                >
                  {s}
                </button>
              ))}
              {watchlist.length > 10 && (
                <span className="font-mono text-[11px] text-mist/40 py-1">+{watchlist.length - 10} more</span>
              )}
            </div>
          )}
        </section>

        {/* Results */}
        <section>
          {view.mode === "idle" && (
            <div className="border border-dashed border-slate rounded-xl p-16 text-center">
              <p className="text-mist/40 font-mono text-xs">Search a symbol or run the scanner to begin.</p>
            </div>
          )}

          {view.mode === "loading" && (
            <div className="rounded-xl border border-slate bg-graphite p-8 max-w-sm">
              <p className="font-mono text-xs text-mist mb-6">{view.label}</p>
              <Pipeline running={true} />
            </div>
          )}

          {view.mode === "error" && (
            <div className="rounded-xl border border-signal-sell/40 bg-signal-sell/5 p-6">
              <p className="font-mono text-xs text-signal-sell/70 uppercase tracking-widest mb-1">Error</p>
              <p className="text-sm text-signal-sell">{view.message}</p>
              <button
                onClick={() => setView({ mode: "idle" })}
                className="mt-4 font-mono text-xs text-mist hover:text-paper underline"
              >
                Try again
              </button>
            </div>
          )}

          {view.mode === "stock" && (
            <DecisionCard
              data={view.data}
              onBack={() => setView({ mode: "idle" })}
              onSearchRelated={handleSearch}
            />
          )}

          {view.mode === "scan" && (
            <ScanPanel
              result={view.data}
              onSelect={handleSearch}
              onBack={() => setView({ mode: "idle" })}
            />
          )}
        </section>
      </main>

      <footer className="max-w-6xl mx-auto px-6 py-6 border-t border-slate/40 mt-12">
        <p className="text-[11px] text-mist/40 font-mono">
          For informational use only — not investment advice. Always verify before trading.
        </p>
      </footer>
    </div>
  );
}