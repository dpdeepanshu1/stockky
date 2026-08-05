import { Decision } from "../api";
import { decisionStyle } from "../decisionStyle";

export default function DecisionCard({ data }: { data: Decision }) {
  const style = decisionStyle[data.decision] ?? decisionStyle["DO NOT BUY"];

  return (
    <div className={`rounded-xl border ${style.border} ${style.bg} p-8`}>
      <div className="flex items-start justify-between gap-6 flex-wrap">
        <div>
          <div className="font-mono text-xs text-mist tracking-widest uppercase mb-2">
            {data.symbol} · {data.sector || "—"}
          </div>
          <h2 className={`font-display text-5xl leading-none ${style.color}`}>
            {data.decision}
          </h2>
          <p className="text-mist mt-2 text-sm">{style.verb} · {data.confidence} confidence</p>
        </div>
        <div className="text-right font-mono">
          <div className="text-3xl text-paper">₹{data.close.toLocaleString("en-IN")}</div>
          <div className="text-xs text-mist mt-1">Combined score {data.combined_score}/100</div>
        </div>
      </div>

      {data.event_risk && (
        <div className="mt-6 rounded-lg border border-signal-hold/40 bg-signal-hold/10 px-4 py-3 text-sm text-signal-hold font-mono">
          ⚠ {data.reasons.event?.[0] || "Upcoming corporate event — elevated near-term risk"}
        </div>
      )}

      {data.decision === "BUY NOW" || data.decision === "PREPARE TO BUY" ? (
        <div className="grid grid-cols-3 gap-4 mt-8 font-mono">
          <Stat label="Entry range" value={`₹${data.entry_range.low} – ₹${data.entry_range.high}`} />
          <Stat label="Target" value={`₹${data.target}`} accent="text-signal-buy" />
          <Stat label="Stop loss" value={`₹${data.stop_loss}`} accent="text-signal-sell" />
        </div>
      ) : null}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-6">
        <ScoreBar label="Technical" value={data.technical_score} />
        <ScoreBar label="Fundamental" value={data.fundamental_score} />
        {data.news_score !== null && <ScoreBar label="News" value={data.news_score} />}
        {data.prediction_score !== null && <ScoreBar label="AI model" value={data.prediction_score} />}
      </div>

      <div className="grid md:grid-cols-2 gap-6 mt-8 pt-6 border-t border-slate/60">
        <ReasonList title="Technical reasoning" items={data.reasons.technical} />
        <ReasonList title="Fundamental reasoning" items={data.reasons.fundamental} />
        {data.reasons.news && data.reasons.news.length > 0 && (
          <ReasonList title="News reasoning" items={data.reasons.news} />
        )}
        {data.reasons.prediction && data.reasons.prediction.length > 0 && (
          <ReasonList title="Prediction model" items={data.reasons.prediction} />
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, accent }: { label: string; value: string; accent?: string }) {
  return (
    <div>
      <div className="text-xs text-mist uppercase tracking-wide">{label}</div>
      <div className={`text-lg mt-1 ${accent || "text-paper"}`}>{value}</div>
    </div>
  );
}

function ScoreBar({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div className="flex justify-between text-xs font-mono text-mist mb-1">
        <span className="uppercase tracking-wide">{label}</span>
        <span>{value}/100</span>
      </div>
      <div className="h-1.5 rounded-full bg-slate overflow-hidden">
        <div
          className="h-full bg-signal-prepare rounded-full transition-all duration-700"
          style={{ width: `${value}%` }}
        />
      </div>
    </div>
  );
}

function ReasonList({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <h3 className="font-display text-lg text-paper mb-3">{title}</h3>
      <ul className="space-y-2">
        {items.map((item, i) => (
          <li key={i} className="text-sm text-mist flex gap-2">
            <span className="text-slate mt-1">—</span>
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
