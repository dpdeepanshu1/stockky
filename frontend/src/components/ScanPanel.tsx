import { ScanResult } from "../api";
import { decisionStyle } from "../decisionStyle";

export default function ScanPanel({
  result,
  onSelect,
}: {
  result: ScanResult;
  onSelect: (symbol: string) => void;
}) {
  if (result.recommendations.length === 0) {
    return (
      <div className="rounded-xl border border-slate bg-graphite p-8 text-center">
        <div className="font-display text-3xl text-signal-avoid mb-2">
          DO NOT BUY ANY STOCK TODAY
        </div>
        <p className="text-mist text-sm">
          {result.scanned} stocks scanned. None cleared the conviction bar. Waiting is the decision.
        </p>
      </div>
    );
  }

  return (
    <div>
      <div className="text-xs font-mono text-mist uppercase tracking-widest mb-3">
        {result.verdict} · {result.scanned} scanned
      </div>
      <div className="grid md:grid-cols-3 gap-4">
        {result.recommendations.map((r) => {
          const style = decisionStyle[r.decision];
          return (
            <button
              key={r.symbol}
              onClick={() => onSelect(r.symbol)}
              className={`text-left rounded-lg border ${style.border} ${style.bg} p-5 hover:brightness-125 transition`}
            >
              <div className="font-mono text-xs text-mist mb-1">{r.symbol}</div>
              <div className={`font-display text-xl ${style.color}`}>{r.decision}</div>
              <div className="text-xs text-mist mt-2">Score {r.combined_score}/100 · {r.confidence}</div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
