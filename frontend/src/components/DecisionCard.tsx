import { Decision } from "../api";
import { decisionStyle } from "../decisionStyle";

interface Props {
  data: Decision;
  onBack: () => void;
  onSearchRelated: (symbol: string) => void;
}

export default function DecisionCard({ data, onBack }: Props) {
  const style = decisionStyle[data.decision] ?? decisionStyle["DO NOT BUY"];
  const isBullish = data.decision === "BUY NOW" || data.decision === "PREPARE TO BUY";

  const scores = [
    { label: "Technical", value: data.technical_score },
    { label: "Fundamental", value: data.fundamental_score },
    ...(data.news_score !== null ? [{ label: "News", value: data.news_score }] : []),
    ...(data.prediction_score !== null ? [{ label: "AI Model", value: data.prediction_score }] : []),
  ];

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
            <div className="text-4xl text-paper">₹{data.close.toLocaleString("en-IN")}</div>
            <div className="text-xs text-mist/60 mt-1">
              Combined {data.combined_score}/100
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
        {isBullish && (
          <div className="grid grid-cols-3 gap-4 mt-8 pt-6 border-t border-slate/40">
            <div>
              <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-1">Entry range</div>
              <div className="font-mono text-sm text-paper">₹{data.entry_range.low.toLocaleString("en-IN")} – ₹{data.entry_range.high.toLocaleString("en-IN")}</div>
            </div>
            <div>
              <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-1">Target</div>
              <div className="font-mono text-sm text-signal-buy">₹{data.target.toLocaleString("en-IN")}</div>
              <div className="font-mono text-[10px] text-mist/50 mt-0.5">
                +{(((data.target - data.close) / data.close) * 100).toFixed(1)}%
              </div>
            </div>
            <div>
              <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-1">Stop loss</div>
              <div className="font-mono text-sm text-signal-sell">₹{data.stop_loss.toLocaleString("en-IN")}</div>
              <div className="font-mono text-[10px] text-mist/50 mt-0.5">
                -{(((data.close - data.stop_loss) / data.close) * 100).toFixed(1)}%
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Price levels */}
      <div className="grid grid-cols-2 gap-4">
        <div className="rounded-xl border border-slate bg-graphite p-5">
          <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">Price levels</div>
          <PriceLevelBar close={data.close} support={data.support} resistance={data.resistance} />
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

      {/* Reasons */}
      <div className="grid md:grid-cols-2 gap-4">
        <ReasonList title="Technical" items={data.reasons.technical} />
        <ReasonList title="Fundamental" items={data.reasons.fundamental} />
        {data.reasons.news && data.reasons.news.length > 0 && (
          <ReasonList title="News" items={data.reasons.news} />
        )}
        {data.reasons.prediction && data.reasons.prediction.length > 0 && (
          <ReasonList title="AI Prediction" items={data.reasons.prediction} />
        )}
      </div>

      {/* Holding period */}
      {data.holding_period !== "N/A" && (
        <div className="rounded-xl border border-slate bg-graphite px-5 py-4 font-mono text-xs text-mist flex justify-between">
          <span className="uppercase tracking-widest">Suggested holding period</span>
          <span className="text-paper">{data.holding_period}</span>
        </div>
      )}

      {/* 🔥 NEW: Natural‑language Hinglish summary */}
      <div className="rounded-xl border border-slate/60 bg-graphite/50 p-5">
        <h4 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
          💬 Final Remarks
        </h4>
        <p className="text-sm text-paper/90 leading-relaxed">
          {data.natural_language_summary}
        </p>
      </div>
    </div>
  );
}

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
  return (
    <div className="rounded-xl border border-slate bg-graphite p-5">
      <h3 className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">{title}</h3>
      <ul className="space-y-2">
        {items.map((item, i) => (
          <li key={i} className="text-sm text-mist/80 flex gap-2 leading-relaxed">
            <span className="text-slate mt-1 shrink-0">–</span>
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}