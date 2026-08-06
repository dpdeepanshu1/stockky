import { useState, useEffect } from "react";
import { api, getApiUrl, setApiUrl, Decision, ScanResult } from "./api";
import Pipeline from "./components/Pipeline";
import DecisionCard from "./components/DecisionCard";
import ScanPanel from "./components/ScanPanel";
import WatchlistManager from "./components/WatchlistManager";
import NotificationsPanel from "./components/NotificationsPanel";

type ViewState =
  | { mode: "idle" }
  | { mode: "loading"; label: string }
  | { mode: "stock"; data: Decision }
  | { mode: "scan"; data: ScanResult }
  | { mode: "error"; message: string };

type Tab = "dashboard" | "notifications";

export default function App() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const [query, setQuery] = useState("");
  const [view, setView] = useState<ViewState>({ mode: "idle" });
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [showWatchlist, setShowWatchlist] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [backendUp, setBackendUp] = useState<"checking" | "up" | "down">("checking");

  useEffect(() => {
    checkBackend();
  }, []);

  function checkBackend() {
    setBackendUp("checking");
    api
      .ping()
      .then(() => {
        setBackendUp("up");
        api.getWatchlist().then((r) => setWatchlist(r.symbols)).catch(() => {});
      })
      .catch(() => setBackendUp("down"));
  }

  async function handleSearch(symbol: string) {
    if (!symbol.trim()) return;
    setTab("dashboard");
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
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-4 flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-4">
            <div>
              <span className="font-display text-xl tracking-tight">Stockky</span>
              <span className="font-mono text-[10px] text-mist tracking-widest uppercase ml-3 hidden sm:inline">
                NSE · India
              </span>
            </div>
            <nav className="flex items-center gap-1 ml-2">
              <TabButton active={tab === "dashboard"} onClick={() => setTab("dashboard")}>
                Dashboard
              </TabButton>
              <TabButton active={tab === "notifications"} onClick={() => setTab("notifications")}>
                Notifications
              </TabButton>
            </nav>
          </div>
          <div className="flex items-center gap-2">
            <BackendStatusDot status={backendUp} onClick={() => setShowSettings(true)} />
            {tab === "dashboard" && (
              <button
                onClick={() => setShowWatchlist(!showWatchlist)}
                className="flex items-center gap-2 text-xs font-mono text-mist hover:text-paper border border-slate rounded-lg px-3 py-2 hover:border-mist/60 transition"
              >
                <span className="w-1.5 h-1.5 rounded-full bg-signal-prepare inline-block" />
                Watchlist ({watchlist.length})
              </button>
            )}
            <button
              onClick={() => setShowSettings(!showSettings)}
              className="text-xs font-mono text-mist hover:text-paper border border-slate rounded-lg px-3 py-2 hover:border-mist/60 transition"
              title="Backend settings"
            >
              ⚙
            </button>
          </div>
        </div>
      </header>

      {showSettings && (
        <SettingsBanner onClose={() => setShowSettings(false)} onSaved={checkBackend} />
      )}

      {backendUp === "down" && !showSettings && (
        <div className="border-b border-signal-sell/30 bg-signal-sell/5">
          <div className="max-w-6xl mx-auto px-4 sm:px-6 py-3 flex items-center justify-between gap-3 flex-wrap">
            <p className="font-mono text-xs text-signal-sell">
              Can't reach the backend at <span className="text-signal-sell/80">{getApiUrl() || "(not set)"}</span>.
            </p>
            <div className="flex gap-3">
              <button onClick={checkBackend} className="font-mono text-xs text-mist hover:text-paper underline">
                Retry
              </button>
              <button
                onClick={() => setShowSettings(true)}
                className="font-mono text-xs text-signal-sell hover:text-paper underline"
              >
                Fix in Settings
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Watchlist drawer */}
      {showWatchlist && tab === "dashboard" && (
        <div className="border-b border-slate/60 bg-graphite">
          <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6">
            <WatchlistManager
              symbols={watchlist}
              onChange={handleWatchlistUpdate}
              onAnalyse={(s) => {
                setShowWatchlist(false);
                handleSearch(s);
              }}
            />
          </div>
        </div>
      )}

      <main className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        {tab === "notifications" ? (
          <NotificationsPanel />
        ) : (
          <>
            {/* Hero */}
            <section className="mb-8 sm:mb-10">
              <h1 className="font-display text-3xl sm:text-4xl md:text-[46px] leading-tight max-w-xl mb-2">
                Know your next move. <span className="italic text-mist">In one call.</span>
              </h1>
              <p className="text-mist text-sm max-w-lg mb-8">
                Technical, fundamental, news and AI signals — combined into a single decision.
              </p>

              <div className="flex flex-col sm:flex-row gap-3">
                <div className="flex-1 flex items-center gap-2 border border-slate rounded-xl px-4 py-3.5 bg-graphite focus-within:border-signal-prepare/60 transition">
                  <span className="font-mono text-mist text-xs select-none">NSE:</span>
                  <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value.toUpperCase())}
                    onKeyDown={(e) => e.key === "Enter" && handleSearch(query)}
                    placeholder="TCS, INFY, RELIANCE…"
                    className="bg-transparent outline-none flex-1 font-mono text-sm placeholder:text-mist/30 min-w-0"
                    autoComplete="off"
                    spellCheck={false}
                  />
                  {query && (
                    <button
                      onClick={() => handleSearch(query)}
                      className="text-[10px] font-mono uppercase tracking-widest bg-signal-prepare/10 text-signal-prepare border border-signal-prepare/30 rounded px-3 py-1.5 hover:bg-signal-prepare/20 transition shrink-0"
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
                    <span className="font-mono text-[11px] text-mist/40 py-1">
                      +{watchlist.length - 10} more
                    </span>
                  )}
                </div>
              )}
            </section>

            {/* Results */}
            <section>
              {view.mode === "idle" && (
                <div className="border border-dashed border-slate rounded-xl p-10 sm:p-16 text-center">
                  <p className="text-mist/40 font-mono text-xs">
                    Search a symbol or run the scanner to begin.
                  </p>
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
                  <p className="font-mono text-xs text-signal-sell/70 uppercase tracking-widest mb-1">
                    Error
                  </p>
                  <p className="text-sm text-signal-sell break-words">{view.message}</p>
                  <div className="flex gap-4 mt-4">
                    <button
                      onClick={() => setView({ mode: "idle" })}
                      className="font-mono text-xs text-mist hover:text-paper underline"
                    >
                      Try again
                    </button>
                    <button
                      onClick={() => setShowSettings(true)}
                      className="font-mono text-xs text-mist hover:text-paper underline"
                    >
                      Check backend settings
                    </button>
                  </div>
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
          </>
        )}
      </main>

      <footer className="max-w-6xl mx-auto px-4 sm:px-6 py-6 border-t border-slate/40 mt-12">
        <p className="text-[11px] text-mist/40 font-mono">
          For informational use only — not investment advice. Always verify before trading.
        </p>
      </footer>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`font-mono text-xs uppercase tracking-widest px-3 py-2 rounded-lg transition ${
        active ? "text-paper bg-slate/60" : "text-mist hover:text-paper"
      }`}
    >
      {children}
    </button>
  );
}

function BackendStatusDot({
  status,
  onClick,
}: {
  status: "checking" | "up" | "down";
  onClick: () => void;
}) {
  const color =
    status === "up" ? "bg-signal-buy" : status === "down" ? "bg-signal-sell" : "bg-signal-hold animate-pulse";
  const label = status === "up" ? "Backend connected" : status === "down" ? "Backend unreachable" : "Checking…";
  return (
    <button
      onClick={onClick}
      title={label}
      className="hidden sm:flex items-center gap-1.5 font-mono text-[10px] text-mist/60 hover:text-mist transition px-1"
    >
      <span className={`w-1.5 h-1.5 rounded-full ${color}`} />
      {label}
    </button>
  );
}

function SettingsBanner({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [url, setUrl] = useState(getApiUrl());

  function save() {
    setApiUrl(url);
    onSaved();
    onClose();
  }

  return (
    <div className="border-b border-slate/60 bg-graphite">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-5">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="flex-1 min-w-[260px]">
            <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
              Backend connection
            </h3>
            <p className="text-mist/70 text-xs mb-3 max-w-md">
              This is the URL of your deployed API Gateway service. If the app shows "Failed to
              fetch", it usually means this wasn't set when the frontend was built — set it here
              once and it's remembered on this device.
            </p>
            <div className="flex gap-2">
              <input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && save()}
                placeholder="https://your-api-gateway.onrender.com"
                className="flex-1 bg-ink/60 border border-slate rounded-lg px-3 py-2 font-mono text-xs text-paper placeholder:text-mist/30 outline-none focus:border-signal-prepare/60 transition"
                spellCheck={false}
                autoComplete="off"
              />
              <button
                onClick={save}
                className="border border-slate rounded-lg px-4 py-2 font-mono text-xs text-mist hover:text-paper hover:border-signal-prepare/60 transition"
              >
                Save
              </button>
            </div>
          </div>
          <button
            onClick={onClose}
            className="font-mono text-xs text-mist hover:text-paper underline shrink-0"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}