// frontend/src/components/Trades.tsx
//
// Portfolio-style Trades tab. Trades share one dummy-money balance
// (trades.py's PortfolioAccount) instead of a fixed pot per trade —
// opening a position locks capital out of cash_balance, closing returns
// the full exit value. Add funds any time; the balance reflects real P&L
// as positions close.

import { useEffect, useState, useCallback } from "react";
import { api, PaperTrade, PortfolioSummary, TradeReportBucket } from "../api";
import StockChart from "./StockChart";

const fmtMoney = (n: number | null | undefined) =>
  n == null ? "—" : `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
const fmtPct = (n: number | null | undefined) => (n == null ? "—" : `${n > 0 ? "+" : ""}${n}%`);
const fmtDate = (s: string | null) =>
  s
    ? new Date(s).toLocaleString("en-IN", {
        day: "2-digit", month: "short", year: "numeric",
        hour: "2-digit", minute: "2-digit",
      })
    : "—";
const daysHeld = (entryDate: string) =>
  Math.max(0, Math.floor((Date.now() - new Date(entryDate).getTime()) / 86400000));

type Toast = { type: "success" | "error" | "info"; message: string };

export default function Trades() {
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [openTrades, setOpenTrades] = useState<PaperTrade[]>([]);
  const [closedTrades, setClosedTrades] = useState<PaperTrade[]>([]);
  const [dailyReport, setDailyReport] = useState<TradeReportBucket[]>([]);
  const [weeklyReport, setWeeklyReport] = useState<TradeReportBucket[]>([]);
  const [loading, setLoading] = useState(true);
  const [markingToMarket, setMarkingToMarket] = useState(false);
  const [closingId, setClosingId] = useState<string | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);
  const [tab, setTab] = useState<"open" | "closed" | "daily" | "weekly">("open");
  const [expandedSymbol, setExpandedSymbol] = useState<string | null>(null);
  const [showDeposit, setShowDeposit] = useState(false);
  const [depositAmount, setDepositAmount] = useState("10000");
  const [depositing, setDepositing] = useState(false);

  const showToast = (type: Toast["type"], message: string) => {
    setToast({ type, message });
    setTimeout(() => setToast(null), 4000);
  };

  const fetchAll = useCallback(async () => {
    setLoading(true);
    const [summaryR, openR, closedR, dailyR, weeklyR] = await Promise.allSettled([
      api.getPortfolioSummary(),
      api.getTrades("open"),
      api.getTrades("closed"),
      api.getDailyTradeReport(30),
      api.getWeeklyTradeReport(12),
    ]);
    const failed: string[] = [];
    if (summaryR.status === "fulfilled") setSummary(summaryR.value);
    else failed.push("portfolio summary");
    if (openR.status === "fulfilled") setOpenTrades(openR.value || []);
    else failed.push("open trades");
    if (closedR.status === "fulfilled") setClosedTrades(closedR.value || []);
    else failed.push("closed trades");
    if (dailyR.status === "fulfilled") setDailyReport(dailyR.value || []);
    else failed.push("daily report");
    if (weeklyR.status === "fulfilled") setWeeklyReport(weeklyR.value || []);
    else failed.push("weekly report");
    if (failed.length > 0) {
      [summaryR, openR, closedR, dailyR, weeklyR].forEach((r) => {
        if (r.status === "rejected") console.error("Trades fetch failed:", r.reason);
      });
      showToast("error", `Couldn't load: ${failed.join(", ")} — rest of the page still loaded fine.`);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const runMarkToMarket = async () => {
    setMarkingToMarket(true);
    try {
      await api.markTradesToMarket();
      showToast("info", "Mark-to-market sweep started — refreshing shortly");
      setTimeout(fetchAll, 4000);
    } catch (err) {
      console.error(err);
      showToast("error", `Mark-to-market failed: ${(err as Error).message || "unknown error"}`);
    } finally {
      setMarkingToMarket(false);
    }
  };

  const closeTrade = async (tradeId: string) => {
    setClosingId(tradeId);
    try {
      const data = await api.closeTrade(tradeId);
      showToast(
        data.pnl_pct >= 0 ? "success" : "info",
        `Closed ${tradeId} at ₹${data.exit_price} (${fmtPct(data.pnl_pct)})`
      );
      fetchAll();
    } catch (err) {
      console.error(err);
      showToast("error", `Close trade failed: ${(err as Error).message || "unknown error"}`);
    } finally {
      setClosingId(null);
    }
  };

  const submitDeposit = async () => {
    const amount = parseFloat(depositAmount);
    if (!amount || amount <= 0) {
      showToast("error", "Enter a valid amount");
      return;
    }
    setDepositing(true);
    try {
      const result = await api.depositFunds(amount, "Manual top-up");
      showToast("success", `Added ₹${amount.toLocaleString("en-IN")} — balance now ${fmtMoney(result.cash_balance)}`);
      setShowDeposit(false);
      setDepositAmount("10000");
      fetchAll();
    } catch (err) {
      console.error(err);
      showToast("error", `Deposit failed: ${(err as Error).message || "unknown error"}`);
    } finally {
      setDepositing(false);
    }
  };

  return (
    <div className="max-w-5xl mx-auto p-6 space-y-6">
      {toast && (
        <div
          className={`fixed top-4 right-4 z-50 px-4 py-3 rounded-lg font-mono text-sm border shadow-lg ${
            toast.type === "success"
              ? "bg-emerald-500/10 border-emerald-500/40 text-emerald-400"
              : toast.type === "error"
              ? "bg-red-500/10 border-red-500/40 text-red-400"
              : "bg-slate/20 border-slate/40 text-mist"
          }`}
        >
          {toast.message}
        </div>
      )}

      <div className="flex items-center justify-between">
        <h2 className="font-mono text-sm text-paper uppercase tracking-widest">Portfolio</h2>
        <div className="flex gap-2">
          <button
            onClick={() => setShowDeposit(true)}
            className="text-xs font-mono uppercase tracking-wider bg-emerald-500/15 border border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/25 rounded-lg px-3 py-2 transition"
          >
            + Add Funds
          </button>
          <button
            onClick={runMarkToMarket}
            disabled={markingToMarket}
            className="text-xs font-mono uppercase tracking-wider bg-slate/40 hover:bg-slate/60 text-paper rounded-lg px-3 py-2 disabled:opacity-40 transition flex items-center gap-2"
          >
            {markingToMarket && <Spinner />}
            {markingToMarket ? "Marking..." : "Mark to Market"}
          </button>
        </div>
      </div>

      {showDeposit && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 backdrop-blur-sm p-4">
          <div className="bg-graphite border border-slate/60 rounded-2xl p-6 w-full max-w-sm">
            <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-4">Add Dummy Funds</h3>
            <div className="flex items-center gap-2 mb-4">
              <span className="font-mono text-lg text-mist">Rs</span>
              <input
                type="number"
                value={depositAmount}
                onChange={(e) => setDepositAmount(e.target.value)}
                className="flex-1 bg-ink/50 border border-slate/40 rounded-lg px-3 py-2 font-mono text-lg text-paper focus:outline-none focus:border-emerald-500/60"
                autoFocus
              />
            </div>
            <div className="flex gap-2 mb-4">
              {[10000, 50000, 100000].map((amt) => (
                <button
                  key={amt}
                  onClick={() => setDepositAmount(String(amt))}
                  className="flex-1 text-[10px] font-mono border border-slate/40 rounded-lg py-1.5 text-mist hover:text-paper hover:border-slate/60 transition"
                >
                  +{(amt / 1000).toFixed(0)}k
                </button>
              ))}
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => setShowDeposit(false)}
                className="flex-1 text-xs font-mono uppercase tracking-wider border border-slate/40 rounded-lg py-2 text-mist hover:text-paper transition"
              >
                Cancel
              </button>
              <button
                onClick={submitDeposit}
                disabled={depositing}
                className="flex-1 text-xs font-mono uppercase tracking-wider bg-emerald-500/20 border border-emerald-500/50 text-emerald-400 rounded-lg py-2 hover:bg-emerald-500/30 transition disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {depositing && <Spinner />}
                {depositing ? "Adding..." : "Confirm"}
              </button>
            </div>
          </div>
        </div>
      )}

      {summary && (
        <div className="bg-gradient-to-br from-graphite to-ink/60 border border-slate/60 rounded-2xl p-6">
          <div className="flex items-baseline justify-between mb-1">
            <span className="font-mono text-[10px] text-mist/60 uppercase tracking-widest">Total Equity</span>
            <span
              className={`font-mono text-xs px-2 py-0.5 rounded-full ${
                summary.realized_pnl >= 0 ? "bg-emerald-500/15 text-emerald-400" : "bg-red-500/15 text-red-400"
              }`}
            >
              {summary.realized_pnl >= 0 ? "Up" : "Down"} {fmtMoney(Math.abs(summary.realized_pnl))} realized
            </span>
          </div>
          <div className="font-display text-4xl text-paper mb-4 tabular-nums">
            {fmtMoney(summary.total_equity)}
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <StatCard label="Cash Balance" value={fmtMoney(summary.cash_balance)} />
            <StatCard label="In Positions" value={fmtMoney(summary.open_positions_value)} />
            <StatCard
              label="Open P&L"
              value={fmtMoney(summary.open_positions_pnl)}
              highlight={summary.open_positions_pnl >= 0 ? "up" : "down"}
            />
            <StatCard label="Win Rate" value={summary.win_rate == null ? "—" : `${summary.win_rate}%`} />
          </div>
        </div>
      )}

      <div className="bg-graphite border border-slate/60 rounded-xl p-5">
        <div className="flex gap-1 bg-ink/40 border border-slate/40 rounded-lg p-0.5 w-fit mb-4 flex-wrap">
          {([
            ["open", `Open (${openTrades.length})`],
            ["closed", `Closed (${closedTrades.length})`],
            ["daily", "Daily Report"],
            ["weekly", "Weekly Report"],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`px-3 py-1 text-xs font-mono uppercase rounded-md transition-colors ${
                tab === key ? "bg-slate/60 text-paper" : "text-mist/50 hover:text-mist"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {tab === "open" &&
          (loading ? (
            <Spinner size="lg" />
          ) : openTrades.length === 0 ? (
            <Empty text="No open positions. Add actionable stocks to training from a market scan to open trades here." />
          ) : (
            <div className="space-y-3">
              {openTrades.map((t) => (
                <PositionCard
                  key={t.trade_id}
                  trade={t}
                  expanded={expandedSymbol === t.trade_id}
                  onToggle={() => setExpandedSymbol(expandedSymbol === t.trade_id ? null : t.trade_id)}
                  onClose={() => closeTrade(t.trade_id)}
                  closing={closingId === t.trade_id}
                />
              ))}
            </div>
          ))}

        {tab === "closed" &&
          (loading ? (
            <Spinner size="lg" />
          ) : closedTrades.length === 0 ? (
            <Empty text="No closed positions yet." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs font-mono">
                <thead>
                  <tr className="text-mist/50 uppercase tracking-wider border-b border-slate/40">
                    <th className="text-left py-2 pr-3">Symbol</th>
                    <th className="text-right py-2 px-2">Entry</th>
                    <th className="text-right py-2 px-2">Exit</th>
                    <th className="text-right py-2 px-2">P&amp;L</th>
                    <th className="text-right py-2 px-2">Reason</th>
                    <th className="text-right py-2 px-2">Entered</th>
                    <th className="text-right py-2 pl-2">Exited</th>
                  </tr>
                </thead>
                <tbody>
                  {closedTrades.map((t) => (
                    <tr key={t.trade_id} className="border-b border-slate/20 text-paper hover:bg-ink/20 transition">
                      <td className="py-2 pr-3">{t.symbol}</td>
                      <td className="text-right py-2 px-2">₹{t.entry_price}</td>
                      <td className="text-right py-2 px-2">{t.exit_price ? `₹${t.exit_price}` : "—"}</td>
                      <td className={`text-right py-2 px-2 ${(t.pnl_pct ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                        {fmtPct(t.pnl_pct)}
                      </td>
                      <td className="text-right py-2 px-2 text-mist/60">{t.exit_reason?.replace(/_/g, " ") ?? "—"}</td>
                      <td className="text-right py-2 px-2 text-mist/50">{fmtDate(t.entry_date)}</td>
                      <td className="text-right py-2 pl-2 text-mist/50">{fmtDate(t.exit_date)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}

        {tab === "daily" && <ReportTable rows={dailyReport} periodLabel="Date" />}
        {tab === "weekly" && <ReportTable rows={weeklyReport} periodLabel="Week" />}
      </div>

      <p className="text-mist/40 text-xs">
        Trades draw from and return to one shared dummy balance — no real orders are placed.
        Target/stop-loss exit immediately when hit; otherwise positions are reviewed every 7 days
        and closed if already up 3%+, held into the next week if not, up to a 21-day cap.
      </p>
    </div>
  );
}

function PositionCard({
  trade, expanded, onToggle, onClose, closing,
}: {
  trade: PaperTrade; expanded: boolean; onToggle: () => void; onClose: () => void; closing: boolean;
}) {
  const isUp = (trade.pnl_pct ?? 0) >= 0;
  const days = daysHeld(trade.entry_date);
  return (
    <div className="border border-slate/40 rounded-xl overflow-hidden transition-all hover:border-slate/60">
      <div onClick={onToggle} className="p-4 cursor-pointer flex items-center justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          <div className={`w-1.5 h-10 rounded-full shrink-0 ${isUp ? "bg-emerald-400" : "bg-red-400"}`} />
          <div className="min-w-0">
            <div className="font-display text-lg text-paper truncate">{trade.symbol}</div>
            <div className="font-mono text-[10px] text-mist/50">
              {trade.quantity} sh @ ₹{trade.entry_price} · {days}d held
            </div>
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className="font-mono text-sm text-paper">{trade.current_price ? `₹${trade.current_price}` : "—"}</div>
          <div className={`font-mono text-xs ${isUp ? "text-emerald-400" : "text-red-400"}`}>
            {fmtPct(trade.pnl_pct)}
          </div>
        </div>
        <span className={`font-mono text-mist/40 text-xs transition-transform duration-200 ${expanded ? "rotate-180" : ""}`}>▾</span>
      </div>
      {expanded && (
        <div className="border-t border-slate/30 p-4 bg-ink/20">
          <StockChart symbol={trade.symbol} compact />
          <div className="grid grid-cols-3 gap-3 mt-3 font-mono text-xs">
            <div>
              <div className="text-mist/40 text-[10px] uppercase">Target</div>
              <div className="text-emerald-400">{trade.target ? `₹${trade.target}` : "—"}</div>
            </div>
            <div>
              <div className="text-mist/40 text-[10px] uppercase">Stop Loss</div>
              <div className="text-red-400">{trade.stop_loss ? `₹${trade.stop_loss}` : "—"}</div>
            </div>
            <div>
              <div className="text-mist/40 text-[10px] uppercase">P&amp;L Amount</div>
              <div className={isUp ? "text-emerald-400" : "text-red-400"}>{fmtMoney(trade.pnl_amount)}</div>
            </div>
          </div>
          <button
            onClick={(e) => { e.stopPropagation(); onClose(); }}
            disabled={closing}
            className="mt-4 w-full text-[10px] uppercase tracking-wider text-mist/60 hover:text-paper border border-slate/40 rounded-lg py-2 disabled:opacity-40 transition"
          >
            {closing ? "Closing..." : "Close Position at Market"}
          </button>
        </div>
      )}
    </div>
  );
}

function ReportTable({ rows, periodLabel }: { rows: TradeReportBucket[]; periodLabel: string }) {
  if (rows.length === 0) return <Empty text="No trade activity in this window yet." />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs font-mono">
        <thead>
          <tr className="text-mist/50 uppercase tracking-wider border-b border-slate/40">
            <th className="text-left py-2 pr-3">{periodLabel}</th>
            <th className="text-right py-2 px-2">Opened</th>
            <th className="text-right py-2 px-2">Closed</th>
            <th className="text-right py-2 px-2">Win Rate</th>
            <th className="text-right py-2 pl-2">Realized P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.period} className="border-b border-slate/20 text-paper hover:bg-ink/20 transition">
              <td className="py-2 pr-3">{r.period}</td>
              <td className="text-right py-2 px-2 text-mist/70">{r.trades_opened}</td>
              <td className="text-right py-2 px-2 text-mist/70">{r.trades_closed}</td>
              <td className="text-right py-2 px-2 text-mist/70">{r.win_rate == null ? "—" : `${r.win_rate}%`}</td>
              <td className={`text-right py-2 pl-2 ${r.realized_pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                {fmtMoney(r.realized_pnl)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <p className="text-mist/40 text-sm py-6 text-center">{text}</p>;
}

function Spinner({ size = "sm" }: { size?: "sm" | "lg" }) {
  const dimension = size === "lg" ? "w-8 h-8 mx-auto my-6" : "w-3.5 h-3.5";
  return <div className={`${dimension} border-2 border-current border-t-transparent rounded-full animate-spin`} />;
}

function StatCard({ label, value, highlight }: { label: string; value: string | number; highlight?: "up" | "down" }) {
  const color = highlight === "up" ? "text-emerald-400" : highlight === "down" ? "text-red-400" : "text-paper";
  return (
    <div className="bg-ink/40 border border-slate/40 rounded-xl px-4 py-3">
      <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">{label}</div>
      <div className={`font-mono text-lg mt-1 ${color}`}>{value}</div>
    </div>
  );
}
