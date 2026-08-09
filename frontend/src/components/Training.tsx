// frontend/src/components/Training.tsx

import { useEffect, useState, useRef } from "react";
import { api, TrainingStatusResponse } from "../api";

export default function Training() {
  const [status, setStatus] = useState<TrainingStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [training, setTraining] = useState(false);
  const [trainingStartTime, setTrainingStartTime] = useState<Date | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [showFolds, setShowFolds] = useState(false);
  const [toast, setToast] = useState<{ type: "success" | "error" | "info"; message: string } | null>(null);
  
  const timerIntervalRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollIntervalRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const ESTIMATED_TOTAL_SECONDS = 300; // 5 minutes

  const fetchStatus = async () => {
    try {
      setLoading(true);
      const data = await api.getTrainingStatus();
      setStatus(data);
      
      // If training was in progress and now model exists with recent timestamp, stop training state
      if (training && data.production_model_exists && data.last_training) {
        const lastTrainingDate = new Date(data.last_training);
        const now = new Date();
        if ((now.getTime() - lastTrainingDate.getTime()) < 120000) {
          stopTraining(true);
        }
      }
    } catch (err) {
      showToast("error", "Failed to fetch training status. Please refresh.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchStatus();
    return () => {
      if (timerIntervalRef.current) clearInterval(timerIntervalRef.current);
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    };
  }, []);

  const showToast = (type: "success" | "error" | "info", message: string) => {
    setToast({ type, message });
    setTimeout(() => setToast(null), 5000);
  };

  const startTraining = () => {
    setTraining(true);
    setTrainingStartTime(new Date());
    setElapsedSeconds(0);
    
    if (timerIntervalRef.current) clearInterval(timerIntervalRef.current);
    timerIntervalRef.current = setInterval(() => {
      setElapsedSeconds(prev => prev + 1);
    }, 1000);

    if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    pollIntervalRef.current = setInterval(() => {
      fetchStatus();
    }, 5000);
  };

  const stopTraining = (success: boolean) => {
    setTraining(false);
    setTrainingStartTime(null);
    if (timerIntervalRef.current) {
      clearInterval(timerIntervalRef.current);
      timerIntervalRef.current = null;
    }
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
    if (success) {
      showToast("success", "✅ Training completed successfully! Model is deployed.");
      fetchStatus();
    } else {
      showToast("error", "❌ Training failed or was interrupted.");
    }
  };

  const handleTriggerTraining = async () => {
    if (training) return;
    
    showToast("info", "⏳ Starting training...");
    
    try {
      const response = await api.triggerTraining();
      if (response.status === "started" || response.status === "Training started successfully") {
        startTraining();
        showToast("info", "⏳ Training started. This may take a few minutes.");
      } else {
        showToast("error", `⚠️ Training failed: ${response.status}`);
      }
    } catch (err: any) {
      if (err?.status === 409 || err?.message?.includes("409")) {
        showToast("info", "⏳ Training already in progress. Resuming monitoring...");
        startTraining();
      } else {
        showToast("error", "❌ Failed to trigger training. Please try again.");
      }
    }
  };

  const formatDate = (dateStr: string | null | undefined) => {
    if (!dateStr) return "Never";
    return new Date(dateStr).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  };

  const getEstimatedRemaining = () => {
    const remaining = Math.max(0, ESTIMATED_TOTAL_SECONDS - elapsedSeconds);
    if (remaining <= 0) return "Almost done...";
    return formatTime(remaining);
  };

  const renderMetrics = (metrics: Record<string, number>) => {
    if (!metrics || Object.keys(metrics).length === 0) {
      return <p className="text-mist/40 text-sm">No walk‑forward metrics available.</p>;
    }

    const metricLabels: Record<string, string> = {
      SharpeRatio: "Sharpe Ratio",
      SortinoRatio: "Sortino Ratio",
      MaximumDrawdown: "Max Drawdown",
      MaximumDrawdownDuration: "Max Drawdown Duration (days)",
      WinRate: "Win Rate",
      ProfitFactor: "Profit Factor",
      CumulativeReturn: "Cumulative Return",
      DirectionalAccuracy: "Directional Accuracy",
      RMSE: "RMSE",
      MAE: "MAE",
    };

    const formatValue = (key: string, value: number) => {
      if (key === "MaximumDrawdown" || key === "CumulativeReturn" || key === "WinRate" || key === "DirectionalAccuracy") {
        return (value * 100).toFixed(2) + "%";
      }
      if (key === "ProfitFactor" || key === "SharpeRatio" || key === "SortinoRatio") {
        return value.toFixed(3);
      }
      return value.toFixed(4);
    };

    return (
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mt-2">
        {Object.entries(metricLabels).map(([key, label]) => {
          const val = metrics[key];
          if (val === undefined || val === null) return null;
          return (
            <div key={key} className="bg-ink/40 border border-slate/40 rounded-lg px-3 py-2">
              <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">{label}</div>
              <div className="font-mono text-sm text-paper mt-0.5">{formatValue(key, val)}</div>
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div className="space-y-6">
      {/* Toast Alert */}
      {toast && (
        <div
          className={`fixed top-20 right-4 z-50 px-5 py-3 rounded-xl shadow-2xl font-mono text-sm flex items-center gap-3 transition-all duration-300 transform ${
            toast.type === "success"
              ? "bg-green-500/20 border border-green-400/40 text-green-400"
              : toast.type === "error"
              ? "bg-red-500/20 border border-red-400/40 text-red-400"
              : "bg-blue-500/20 border border-blue-400/40 text-blue-400"
          } animate-slideIn`}
        >
          <span>{toast.message}</span>
          <button
            onClick={() => setToast(null)}
            className="ml-2 text-mist/60 hover:text-paper transition"
          >
            ✕
          </button>
        </div>
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="font-display text-2xl text-paper">🧠 Training Intelligence</h2>
        <button
          onClick={handleTriggerTraining}
          disabled={training}
          className={`font-mono text-sm px-5 py-2 rounded-lg transition-all ${
            training
              ? "bg-slate/30 text-mist/50 cursor-not-allowed"
              : "bg-signal-prepare/20 text-signal-prepare border border-signal-prepare/30 hover:bg-signal-prepare/30"
          }`}
        >
          {training ? (
            <span className="flex items-center gap-2">
              <Spinner />
              Training...
            </span>
          ) : (
            "⚡ Trigger Training"
          )}
        </button>
      </div>

      {/* Training in progress card */}
      {training && (
        <div className="bg-graphite border border-signal-prepare/30 rounded-xl p-5 animate-pulse">
          <div className="flex items-center gap-4">
            <Spinner size="lg" />
            <div>
              <h3 className="font-display text-lg text-signal-prepare">Training in progress...</h3>
              <div className="flex flex-wrap gap-6 mt-2 text-sm">
                <div>
                  <span className="text-mist/60">Elapsed: </span>
                  <span className="font-mono text-paper">{formatTime(elapsedSeconds)}</span>
                </div>
                <div>
                  <span className="text-mist/60">Estimated remaining: </span>
                  <span className="font-mono text-paper">{getEstimatedRemaining()}</span>
                </div>
              </div>
              <div className="mt-2 text-xs text-mist/40">
                This may take a few minutes. The page will auto‑update when done.
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Status Cards */}
      {loading ? (
        <div className="flex justify-center py-12">
          <Spinner size="lg" />
        </div>
      ) : (
        <div className="space-y-6">
          {/* Production Model */}
          <div className="bg-graphite border border-slate rounded-xl p-5">
            <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
              📦 Production Model
            </h3>
            {status?.production_model_exists ? (
              <div>
                <span className="font-mono text-sm text-signal-buy">✅ Deployed</span>
                <div className="mt-2 text-xs text-mist/60">
                  Last training: {formatDate(status?.last_training)}
                  {status?.model_version && (
                    <span className="ml-4 text-mist/40">Version: {status.model_version}</span>
                  )}
                </div>
              </div>
            ) : (
              <p className="text-mist/40 text-sm">No production model deployed.</p>
            )}
          </div>

          {/* Overall Stats */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <StatCard label="Last Training" value={formatDate(status?.last_training)} />
            <StatCard label="Dataset Size" value={status?.dataset_size ?? 0} />
            <StatCard label="Symbols" value={status?.num_symbols ?? 0} />
          </div>

          {/* Walk‑Forward Metrics */}
          <div className="bg-graphite border border-slate/60 rounded-xl p-5">
            <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
              📉 Walk‑Forward Performance Metrics
            </h3>
            {renderMetrics(status?.metrics || {})}
          </div>

          {/* Fold Details */}
          {status?.fold_details && status.fold_details.length > 0 && (
            <div className="bg-graphite border border-slate/40 rounded-xl p-5">
              <button
                onClick={() => setShowFolds(!showFolds)}
                className="font-mono text-xs text-mist uppercase tracking-widest flex items-center gap-2 hover:text-paper transition"
              >
                📋 Fold Details
                <span className="text-xs">{showFolds ? "▲" : "▼"}</span>
              </button>
              {showFolds && (
                <div className="mt-3 overflow-x-auto">
                  <table className="w-full text-xs font-mono">
                    <thead>
                      <tr className="text-mist/50 border-b border-slate/40">
                        <th className="text-left py-1 pr-4">Fold</th>
                        <th className="text-left py-1 pr-4">Train Start</th>
                        <th className="text-left py-1 pr-4">Train End</th>
                        <th className="text-left py-1 pr-4">Val Start</th>
                        <th className="text-left py-1 pr-4">Val End</th>
                        <th className="text-left py-1 pr-4">Train Samples</th>
                        <th className="text-left py-1">Val Samples</th>
                      </tr>
                    </thead>
                    <tbody>
                      {status.fold_details.map((fold) => (
                        <tr key={fold.fold} className="border-b border-slate/30">
                          <td className="py-1 pr-4 text-paper">{fold.fold}</td>
                          <td className="py-1 pr-4 text-mist/70">{fold.train_start}</td>
                          <td className="py-1 pr-4 text-mist/70">{fold.train_end}</td>
                          <td className="py-1 pr-4 text-mist/70">{fold.val_start}</td>
                          <td className="py-1 pr-4 text-mist/70">{fold.val_end}</td>
                          <td className="py-1 pr-4 text-mist/70">{fold.train_samples}</td>
                          <td className="py-1 text-mist/70">{fold.val_samples}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Helper Components ──

function Spinner({ size = "sm" }: { size?: "sm" | "lg" }) {
  const dimension = size === "lg" ? "w-8 h-8" : "w-4 h-4";
  return (
    <div className={`${dimension} border-2 border-current border-t-transparent rounded-full animate-spin`} />
  );
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-ink/40 border border-slate/40 rounded-xl px-4 py-3">
      <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">{label}</div>
      <div className="font-mono text-lg text-paper mt-1">{value}</div>
    </div>
  );
}