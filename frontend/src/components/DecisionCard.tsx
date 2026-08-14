import { useState, useEffect } from "react";
import { Decision, api, TrainingScore } from "../api";
import { decisionStyle } from "../decisionStyle";
import StockChart from "./StockChart";
import { toActionablePick } from "./ScanPanel";

interface Props {
  data: Decision;
  onBack: () => void;
  onSearchRelated: (symbol: string) => void;
  onAddToWatchlist: (symbol: string) => void; // NEW Prop
}

export default function DecisionCard({ data, onBack, onSearchRelated, onAddToWatchlist }: Props) {
  const style = decisionStyle[data.decision] ?? decisionStyle["DO NOT BUY"];
  const isBullish = data.decision === "BUY NOW" || data.decision === "PREPARE TO BUY";
  const [isAddingWatchlist, setIsAddingWatchlist] = useState(false);

  // NEW: Trade This
  const [showTradeModal, setShowTradeModal] = useState(false);
  const [tradeCapital, setTradeCapital] = useState("100000");
  const [tradingInProgress, setTradingInProgress] = useState(false);
  const [tradeResult, setTradeResult] = useState<string | null>(null);

  // NEW: model's own trading recommendation (training-service's KNN +
  // classifier signal), fetched lazily since it's a separate call from
  // decision-engine's combined_score.
  const [trainingScore, setTrainingScore] = useState<TrainingScore | null>(null);
  const [loadingTrainingScore, setLoadingTrainingScore] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setTrainingScore(null);
    setLoadingTrainingScore(true);
    api
      .getTrainingScore(data.symbol)
      .then((r) => { if (!cancelled) setTrainingScore(r); })
      .catch(() => { /* no history for this symbol yet, or endpoint unreachable — fine, panel just won't show */ })
      .finally(() => { if (!cancelled) setLoadingTrainingScore(false); });
    return () => { cancelled = true; };
  }, [data.symbol]);

  // ── UPDATED: Include Market Sentiment and Training scores ──
  const scores = [
    { label: "Technical", value: data.technical_score },
    { label: "Fundamental", value: data.fundamental_score },
    ...(data.news_score !== null && data.news_score !== undefined ? [{ label: "News", value: data.news_score }] : []),
    ...(data.prediction_score !== null && data.prediction_score !== undefined ? [{ label: "AI Model", value: data.prediction_score }] : []),
    { label: "Market Sentiment", value: data.market_score ?? 50 },
    { label: "Training", value: data.training_score ?? 50 },
  ];

  const metrics = data.fundamental_metrics;
  const hasMetrics = metrics && Object.values(metrics).some(v => v != null);
  const hasPrice = data.close != null;
  const hasNews = data.reasons.news && data.reasons.news.length > 0;
  const hasEvent = data.reasons.event && data.reasons.event.length > 0;
  const hasMarket = data.reasons.market && data.reasons.market.length > 0;

  const handleAddToWatchlist = async () => {
    if (isAddingWatchlist) return;
    setIsAddingWatchlist(true);
    try {
      await onAddToWatchlist(data.symbol);
    } finally {
      setIsAddingWatchlist(false);
    }
  };

  const handleTradeThis = async () => {
    const capital = parseFloat(tradeCapital);
    if (!capital || capital <= 0) {
      setTradeResult("Enter a valid amount");
      return;
    }
    setTradingInProgress(true);
    setTradeResult(null);
    try {
      const { results } = await api.commitActionablePicks([toActionablePick(data)], capital, true);
      const r = results[0];
      if (r.trade_status === "opened") {
        setTradeResult(`✅ Trade opened (${r.trade_id}) — recorded to training too.`);
      } else if (r.trade_status === "already_open_or_closed") {
        setTradeResult(`Already have a position from today's pick (${r.trade_id}).`);
      } else {
        setTradeResult(`Could not open trade: ${r.trade_status}`);
      }
    } catch (err) {
      console.error(err);
      setTradeResult("Failed to open trade — check the Trades tab / gateway routing.");
    } finally {
      setTradingInProgress(false);
    }
  };

  // ── Helper to format adjustment with sign ──
  const formatAdjustment = (adj: number) => {
    if (adj === 0) return "±0";
    return adj > 0 ? `+${adj}` : `${adj}`;
  };

  return (
    <div className="space-y-4">
      <button
        onClick={onBack}
        className="font-mono text-xs text-mist hover:text-paper transition flex items-center gap-1"
      >
        ← Back
      </button>

      {/* Decision header */}
      <div className={`rounded-2xl border ${style.border} ${style.bg} p-8`}>
        <div className="flex items-start justify-between gap-6 flex-wrap">
          <div>
            <div className="font-mono text-xs text-mist tracking-widest uppercase mb-3 flex items-center gap-2">
              <span>{data.symbol}</span>
              {data.sector && <><span className="text-slate">·</span><span>{data.sector}</span></>}
              {data.valuation && <><span className="text-slate">·</span><span className="text-mist/60">{data.valuation}</span></>}
            </div>
            <h2 className={`font-display text-5xl leading-none ${style.color} mb-2`}>
              {data.decision}
            </h2>
            <p className="text-mist text-sm">{style.verb} · {data.confidence} confidence</p>
          </div>

          <div className="text-right font-mono">
            <div className="text-4xl text-paper">
              {hasPrice ? `₹${data.close!.toLocaleString("en-IN")}` : data.data_insufficient ? "Awaiting Data" : "N/A"}
            </div>
            <div className="text-xs text-mist/60 mt-1 flex items-center justify-end gap-2">
              <span>Combined {data.combined_score}/100</span>
              {/* ── NEW: Show adjustment ── */}
              {data.market_sentiment_adjustment !== undefined && data.market_sentiment_adjustment !== 0 && (
                <span className={`text-xs font-medium ${data.market_sentiment_adjustment > 0 ? 'text-signal-buy' : 'text-signal-sell'}`}>
                  ({formatAdjustment(data.market_sentiment_adjustment)})
                </span>
              )}
            </div>
          </div>
        </div>

        {/* Event risk banner */}
        {data.event_risk && (
          <div className="mt-6 rounded-lg border border-signal-hold/40 bg-signal-hold/10 px-4 py-3 text-sm text-signal-hold font-mono flex items-start gap-2">
            <span>⚠</span>
            <span>{data.reasons.event?.[0] || "Upcoming corporate event — elevated near-term risk"}</span>
          </div>
        )}

        {/* Trade levels */}
        {isBullish && data.entry_range && hasPrice && (
          <div className="grid grid-cols-3 gap-4 mt-8 pt-6 border-t border-slate/40">
            <div>
              <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-1">Entry range</div>
              <div className="font-mono text-sm text-paper">
                ₹{data.entry_range.low?.toLocaleString("en-IN") ?? "N/A"} – 
                ₹{data.entry_range.high?.toLocaleString("en-IN") ?? "N/A"}
              </div>
            </div>
            <div>
              <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-1">Target</div>
              <div className="font-mono text-sm text-signal-buy">
                ₹{data.target?.toLocaleString("en-IN") ?? "N/A"}
              </div>
              {data.target != null && data.close != null && data.close !== 0 && (
                <div className="font-mono text-[10px] text-mist/50 mt-0.5">
                  +{(((data.target - data.close) / data.close) * 100).toFixed(1)}%
                </div>
              )}
            </div>
            <div>
              <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-1">Stop loss</div>
              <div className="font-mono text-sm text-signal-sell">
                ₹{data.stop_loss?.toLocaleString("en-IN") ?? "N/A"}
              </div>
              {data.stop_loss != null && data.close != null && data.close !== 0 && (
                <div className="font-mono text-[10px] text-mist/50 mt-0.5">
                  -{(((data.close - data.stop_loss) / data.close) * 100).toFixed(1)}%
                </div>
              )}
            </div>
          </div>
        )}
        
        {/* Watchlist + Trade buttons */}
        <div className="mt-4 flex justify-end gap-2">
          {isBullish && (
            <button
              onClick={() => setShowTradeModal(true)}
              className="text-[10px] font-mono transition border px-3 py-1 rounded flex items-center gap-1 bg-emerald-500/15 border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/25"
            >
              💰 Trade This
            </button>
          )}
          <button
            onClick={handleAddToWatchlist}
            disabled={isAddingWatchlist}
            className={`text-[10px] font-mono transition border px-3 py-1 rounded flex items-center gap-1 ${
              isAddingWatchlist 
                ? "bg-signal-buy/20 border-signal-buy text-signal-buy" 
                : "text-signal-prepare hover:text-paper border-signal-prepare/30"
            }`}
          >
            {isAddingWatchlist ? (
              <>
                <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-signal-buy animate-spin"></span>
                Adding...
              </>
            ) : (
              "+ Watchlist"
            )}
          </button>
        </div>
      </div>

      {/* Trade This confirmation modal */}
      {showTradeModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 backdrop-blur-sm p-4">
          <div className="bg-graphite border border-slate/60 rounded-2xl p-6 w-full max-w-sm">
            <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-1">
              Trade {data.symbol}
            </h3>
            <p className="text-mist/50 text-xs mb-4">
              Opens a paper trade at ₹{data.close ?? "current price"} using dummy capital from your
              shared portfolio balance, and records this pick to training either way.
            </p>
            <div className="flex items-center gap-2 mb-4">
              <span className="font-mono text-lg text-mist">₹</span>
              <input
                type="number"
                value={tradeCapital}
                onChange={(e) => setTradeCapital(e.target.value)}
                className="flex-1 bg-ink/50 border border-slate/40 rounded-lg px-3 py-2 font-mono text-lg text-paper focus:outline-none focus:border-emerald-500/60"
                autoFocus
              />
            </div>
            {tradeResult && (
              <p className="text-xs font-mono text-mist/70 mb-4">{tradeResult}</p>
            )}
            <div className="flex gap-2">
              <button
                onClick={() => { setShowTradeModal(false); setTradeResult(null); }}
                className="flex-1 text-xs font-mono uppercase tracking-wider border border-slate/40 rounded-lg py-2 text-mist hover:text-paper transition"
              >
                {tradeResult ? "Close" : "Cancel"}
              </button>
              {!tradeResult && (
                <button
                  onClick={handleTradeThis}
                  disabled={tradingInProgress}
                  className="flex-1 text-xs font-mono uppercase tracking-wider bg-emerald-500/20 border border-emerald-500/50 text-emerald-400 rounded-lg py-2 hover:bg-emerald-500/30 transition disabled:opacity-50 flex items-center justify-center gap-2"
                >
                  {tradingInProgress && (
                    <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-emerald-400 animate-spin" />
                  )}
                  {tradingInProgress ? "Opening..." : "Confirm Trade"}
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Price chart */}
      <StockChart symbol={data.symbol} />

      {/* Model's own trading recommendation, from training-service's real
          pick history — separate signal from decision-engine's combined_score,
          based on what actually happened to similar past setups. */}
      {loadingTrainingScore ? (
        <div className="rounded-xl border border-slate/40 bg-graphite/30 p-4 text-xs text-mist/40 font-mono flex items-center gap-2">
          <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-mist/40 animate-spin" />
          Checking model recommendation...
        </div>
      ) : trainingScore ? (
        <div className="rounded-xl border border-signal-prepare/30 bg-graphite p-5">
          <h3 className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">
            🤖 Model Recommendation
          </h3>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-3">
            <MetricItem label="Training Score" value={`${trainingScore.training_score}`} />
            <MetricItem label="T+1 Success" value={`${trainingScore.t1_success_probability}%`} />
            <MetricItem label="T+5 Success" value={`${trainingScore.t5_success_probability}%`} />
            <MetricItem
              label="Model Confidence"
              value={trainingScore.model_success_probability == null ? "—" : `${trainingScore.model_success_probability}%`}
            />
          </div>
          {trainingScore.similar_setups && trainingScore.similar_setups.length > 0 && (
            <div className="text-xs text-mist/60">
              Based on {trainingScore.similar_setups.length} similar past setups in this system's own history.
            </div>
          )}
        </div>
      ) : null}

      {/* Price levels + Score breakdown */}
      <div className="grid grid-cols-2 gap-4">
        <div className="rounded-xl border border-slate bg-graphite p-5">
          <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">Price levels</div>
          {hasPrice && data.support != null && data.resistance != null ? (
            <PriceLevelBar close={data.close!} support={data.support} resistance={data.resistance} />
          ) : (
            <p className="text-sm text-mist/60 italic">
              {data.data_insufficient 
                ? `Insufficient price data for ${data.symbol} (newly listed stock). Please check back in 2-3 days after Yahoo Finance updates its database.` 
                : "Insufficient data for price levels"}
            </p>
          )}
        </div>
        <div className="rounded-xl border border-slate bg-graphite p-5">
          <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">Score breakdown</div>
          <div className="space-y-3">
            {scores.map((s) => (
              <ScoreBar key={s.label} label={s.label} value={s.value} />
            ))}
          </div>
        </div>
      </div>

      {/* Fundamental Metrics */}
      {metrics && (
        <div className="rounded-xl border border-slate/60 bg-graphite/30 p-5">
          <h3 className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">
            📊 Fundamental Metrics
          </h3>
          {data.fundamental_fallback ? (
            <p className="text-sm text-mist/60 italic">
              Live data temporarily unavailable — score is based on last known or default values.
            </p>
          ) : hasMetrics ? (
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
              {metrics.revenue_growth != null && (
                <MetricItem label="Revenue Growth" value={`${metrics.revenue_growth.toFixed(1)}%`} />
              )}
              {metrics.earnings_growth != null && (
                <MetricItem label="Earnings Growth" value={`${metrics.earnings_growth.toFixed(1)}%`} />
              )}
              {metrics.roe != null && (
                <MetricItem label="ROE" value={`${metrics.roe.toFixed(1)}%`} />
              )}
              {metrics.debt_to_equity != null && (
                <MetricItem label="Debt/Equity" value={`${metrics.debt_to_equity.toFixed(1)}`} />
              )}
              {metrics.free_cashflow != null && (
                <MetricItem label="Free Cash Flow" value={metrics.free_cashflow > 0 ? "Positive" : "Negative"} />
              )}
              {metrics.profit_margins != null && (
                <MetricItem label="Net Margin" value={`${metrics.profit_margins.toFixed(1)}%`} />
              )}
              {metrics.institutional_holding != null && (
                <MetricItem label="Institutional Holding" value={`${metrics.institutional_holding.toFixed(1)}%`} />
              )}
              {metrics.pe_ratio != null && (
                <MetricItem label="P/E Ratio" value={`${metrics.pe_ratio.toFixed(1)}`} />
              )}
              {metrics.forward_pe != null && (
                <MetricItem label="Forward P/E" value={`${metrics.forward_pe.toFixed(1)}`} />
              )}
            </div>
          ) : (
            <p className="text-sm text-mist/60 italic">
              No fundamental metrics available for this symbol. The score is based on limited available data.
            </p>
          )}
        </div>
      )}

      {/* Reasons – including News, Event, Market, Training */}
      <div className="grid md:grid-cols-2 gap-4">
        <ReasonList title="Technical" items={data.reasons.technical} />
        <ReasonList title="Fundamental" items={data.reasons.fundamental} />
        {hasNews && <ReasonList title="News" items={data.reasons.news!} />}
        {data.reasons.prediction && data.reasons.prediction.length > 0 && (
          <ReasonList title="AI Prediction" items={data.reasons.prediction} />
        )}
        {hasEvent && <ReasonList title="Event Tracker" items={data.reasons.event!} />}
        {/* ── NEW: Market Sentiment reasons ── */}
        {hasMarket && <ReasonList title="Market Sentiment" items={data.reasons.market!} />}
        {data.reasons.training && data.reasons.training.length > 0 && (
          <ReasonList title="Training Intelligence" items={data.reasons.training} />
        )}
      </div>

      {/* Holding period */}
      {(data.holding_period !== "N/A" || data.holding_period_estimate) && (
        <div className="rounded-xl border border-slate bg-graphite px-5 py-4 font-mono text-xs text-mist">
          <div className="flex justify-between">
            <span className="uppercase tracking-widest">Suggested holding period</span>
            <span className="text-paper">{data.holding_period}</span>
          </div>
          {data.holding_period_estimate && (
            <div className="mt-2 pt-2 border-t border-slate/30 flex justify-between text-mist/70">
              <span className="uppercase tracking-widest text-[10px]">Estimated date range</span>
              <span className="text-paper">{data.holding_period_estimate.label}</span>
            </div>
          )}
        </div>
      )}

      {/* Event update — raw pass-through from event-tracker-service, shape
          isn't fixed on the gateway side, so this renders defensively:
          arrays of strings as bullets, small key/value objects as a list,
          otherwise falls back to compact JSON so nothing is silently lost. */}
      {data.event_data && Object.keys(data.event_data).length > 0 && (
        <div className="rounded-xl border border-slate/60 bg-graphite/50 p-5">
          <h4 className="font-mono text-xs text-mist uppercase tracking-widest mb-3">
            📅 Event Update
          </h4>
          <EventDataView data={data.event_data} />
        </div>
      )}

      {/* Natural-language Hinglish summary */}
      {data.natural_language_summary && (
        <div className="rounded-xl border border-slate/60 bg-graphite/50 p-5">
          <h4 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
            💬 Final Remarks
          </h4>
          <p className="text-sm text-paper/90 leading-relaxed">
            {data.natural_language_summary}
          </p>
        </div>
      )}
    </div>
  );
}

function EventDataView({ data }: { data: Record<string, unknown> }) {
  return (
    <div className="space-y-2">
      {Object.entries(data).map(([key, value]) => {
        if (value == null) return null;
        const label = key.replace(/_/g, " ");
        if (Array.isArray(value)) {
          if (value.length === 0) return null;
          return (
            <div key={key}>
              <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider mb-1">{label}</div>
              <ul className="space-y-1">
                {value.map((item, i) => (
                  <li key={i} className="text-sm text-mist/80 flex gap-2 leading-relaxed">
                    <span className="text-slate mt-1 shrink-0">–</span>
                    <span>{typeof item === "string" ? item : JSON.stringify(item)}</span>
                  </li>
                ))}
              </ul>
            </div>
          );
        }
        if (typeof value === "object") {
          return (
            <div key={key}>
              <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider mb-1">{label}</div>
              <pre className="text-xs text-mist/70 whitespace-pre-wrap font-mono bg-ink/30 rounded p-2">
                {JSON.stringify(value, null, 2)}
              </pre>
            </div>
          );
        }
        return (
          <div key={key} className="flex justify-between text-sm">
            <span className="text-mist/50 font-mono text-[10px] uppercase tracking-wider self-center">{label}</span>
            <span className="text-paper">{String(value)}</span>
          </div>
        );
      })}
    </div>
  );
}

// Helper components (unchanged)
function PriceLevelBar({ close, support, resistance }: { close: number; support: number; resistance: number }) {
  const range = resistance - support;
  const closePct = range > 0 ? ((close - support) / range) * 100 : 50;

  return (
    <div>
      <div className="relative h-2 rounded-full bg-slate overflow-visible mb-3">
        <div
          className="absolute h-full rounded-full bg-gradient-to-r from-signal-sell/30 to-signal-buy/30"
          style={{ width: "100%" }}
        />
        <div
          className="absolute top-1/2 -translate-y-1/2 w-3 h-3 rounded-full bg-paper border-2 border-signal-prepare shadow-lg"
          style={{ left: `${Math.min(95, Math.max(5, closePct))}%`, transform: "translate(-50%, -50%)" }}
        />
      </div>
      <div className="flex justify-between font-mono text-[10px]">
        <span className="text-signal-sell">S ₹{support.toLocaleString("en-IN")}</span>
        <span className="text-signal-prepare">₹{close.toLocaleString("en-IN")}</span>
        <span className="text-signal-buy">R ₹{resistance.toLocaleString("en-IN")}</span>
      </div>
    </div>
  );
}

function ScoreBar({ label, value }: { label: string; value: number }) {
  const color = value >= 70 ? "bg-signal-buy" : value >= 50 ? "bg-signal-prepare" : "bg-signal-sell/60";
  return (
    <div>
      <div className="flex justify-between font-mono text-[10px] text-mist mb-1">
        <span className="uppercase tracking-wide">{label}</span>
        <span className={value >= 70 ? "text-signal-buy" : value >= 50 ? "text-signal-prepare" : "text-signal-sell"}>{value}</span>
      </div>
      <div className="h-1 rounded-full bg-slate overflow-hidden">
        <div className={`h-full rounded-full ${color} transition-all duration-700`} style={{ width: `${value}%` }} />
      </div>
    </div>
  );
}

function ReasonList({ title, items }: { title: string; items: string[] }) {
  const hasItems = items && items.length > 0;

  return (
    <div className="rounded-xl border border-slate bg-graphite p-5">
      <h3 className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">{title}</h3>
      {hasItems ? (
        <ul className="space-y-2">
          {items.map((item, i) => (
            <li key={i} className="text-sm text-mist/80 flex gap-2 leading-relaxed">
              <span className="text-slate mt-1 shrink-0">–</span>
              <span>{item}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-mist/40 italic">No specific {title.toLowerCase()} insights available</p>
      )}
    </div>
  );
}

function MetricItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-ink/40 border border-slate/40 rounded-lg px-3 py-2">
      <div className="font-mono text-[9px] text-mist/50 uppercase tracking-wider">{label}</div>
      <div className="font-mono text-sm text-paper mt-0.5">{value}</div>
    </div>
  );
}