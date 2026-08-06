import { ScanResult, Decision } from "../api";
import { decisionStyle } from "../decisionStyle";

interface Props {
  result: ScanResult;
  onSelect: (symbol: string) => void;
  onBack: () => void;
}

export default function ScanPanel({ result, onSelect, onBack }: Props) {
  const allSorted = [...result.all_results].sort((a, b) => b.combined_score - a.combined_score);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <button
          onClick={onBack}
          className="font-mono text-xs text-mist hover:text-paper transition flex items-center gap-1"
        >
          ← Back
        </button>
        <span className="font-mono text-xs text-mist">{result.scanned} stocks scanned</span>
      </div>

      {/* Verdict banner */}
      {result.recommendations.length === 0 ? (
        <div className="rounded-2xl border border-slate bg-graphite p-10 text-center">
          <div className="font-display text-4xl text-signal-avoid mb-3">DO NOT BUY ANY STOCK TODAY</div>
          <p className="text-mist text-sm max-w-md mx-auto">
            {result.scanned} stocks scanned. None cleared the conviction bar today. Waiting is the decision.
          </p>
        </div>
      ) : (
        <>
          <div className="font-mono text-xs text-mist/60 uppercase tracking-widest">{result.verdict}</div>
          <div className="grid md:grid-cols-3 gap-4">
            {result.recommendations.map((r, i) => (
              <TopPick key={r.symbol} rank={i + 1} data={r} onSelect={onSelect} />
            ))}
          </div>
        </>
      )}

      {/* Full results table */}
      <div>
        <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">All results</div>
        <div className="rounded-xl border border-slate overflow-hidden">
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="border-b border-slate bg-graphite">
                <th className="text-left px-4 py-3 text-[10px] text-mist uppercase tracking-widest">Symbol</th>
                <th className="text-left px-4 py-3 text-[10px] text-mist uppercase tracking-widest">Decision</th>
                <th className="text-right px-4 py-3 text-[10px] text-mist uppercase tracking-widest">Score</th>
                <th className="text-right px-4 py-3 text-[10px] text-mist uppercase tracking-widest">Price</th>
                <th className="text-right px-4 py-3 text-[10px] text-mist uppercase tracking-widest hidden md:table-cell">Technical</th>
                <th className="text-right px-4 py-3 text-[10px] text-mist uppercase tracking-widest hidden md:table-cell">Fundamental</th>
                <th className="px-4 py-3"></th>
              </tr>
            </thead>
            <tbody>
              {allSorted.map((r) => {
                const style = decisionStyle[r.decision];
                return (
                  <tr key={r.symbol} className="border-b border-slate/40 hover:bg-graphite transition">
                    <td className="px-4 py-3 text-paper font-semibold">{r.symbol}</td>
                    <td className={`px-4 py-3 text-xs ${style.color}`}>{r.decision}</td>
                    <td className="px-4 py-3 text-right text-mist">{r.combined_score}</td>
                    <td className="px-4 py-3 text-right text-paper">₹{r.close.toLocaleString("en-IN")}</td>
                    <td className="px-4 py-3 text-right text-mist hidden md:table-cell">{r.technical_score}</td>
                    <td className="px-4 py-3 text-right text-mist hidden md:table-cell">{r.fundamental_score}</td>
                    <td className="px-4 py-3 text-right">
                      <button
                        onClick={() => onSelect(r.symbol)}
                        className="text-[10px] text-signal-prepare hover:text-paper transition uppercase tracking-wide"
                      >
                        View →
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {result.errors.length > 0 && (
        <div className="rounded-xl border border-slate bg-graphite p-4">
          <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-2">Skipped ({result.errors.length})</div>
          <div className="flex flex-wrap gap-2">
            {result.errors.map((e) => (
              <span key={e.symbol} className="font-mono text-xs text-mist/50">{e.symbol}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function TopPick({ rank, data, onSelect }: { rank: number; data: Decision; onSelect: (s: string) => void }) {
  const style = decisionStyle[data.decision];
  const upside = (((data.target - data.close) / data.close) * 100).toFixed(1);

  return (
    <button
      onClick={() => onSelect(data.symbol)}
      className={`text-left rounded-xl border ${style.border} ${style.bg} p-6 hover:brightness-110 transition group`}
    >
      <div className="flex items-start justify-between mb-4">
        <span className="font-mono text-[10px] text-mist/60">#{rank}</span>
        <span className="font-mono text-xs text-signal-buy">+{upside}% target</span>
      </div>
      <div className="font-mono text-sm text-mist mb-1">{data.symbol}</div>
      <div className={`font-display text-2xl ${style.color} mb-3`}>{data.decision}</div>
      <div className="flex justify-between font-mono text-xs text-mist">
        <span>₹{data.close.toLocaleString("en-IN")}</span>
        <span>Score {data.combined_score}/100</span>
      </div>
      <div className="mt-3 pt-3 border-t border-slate/40 font-mono text-[10px] text-mist/50 group-hover:text-mist transition">
        View full analysis →
      </div>
    </button>
  );
}