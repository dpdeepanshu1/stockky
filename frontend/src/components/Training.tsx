// frontend/src/components/Training.tsx

import { useEffect, useState } from "react";
import { api, TrainingModelStatus } from "../api";

export default function Training() {
  const [status, setStatus] = useState<TrainingModelStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [training, setTraining] = useState(false);
  const [toast, setToast] = useState<{ type: "success" | "error" | "info"; message: string } | null>(null);

  const fetchStatus = async () => {
    try {
      setLoading(true);
      const data = await api.getTrainingStatus();
      setStatus(data);
    } catch (err) {
      showToast("error", "Failed to fetch training status. Please refresh.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchStatus();
  }, []);

  const showToast = (type: "success" | "error" | "info", message: string) => {
    setToast({ type, message });
    setTimeout(() => setToast(null), 5000);
  };

  const handleTriggerTraining = async () => {
    if (training) return;
    setTraining(true);
    showToast("info", "⏳ Training started... This may take a minute.");

    try {
      const response = await api.triggerTraining();
      // response is { status: string }
      if (response.status === "success" || response.status === "started") {
        showToast("success", "✅ Training triggered successfully! The model will be updated shortly.");
        setTimeout(() => {
          fetchStatus();
          setTraining(false);
        }, 3000);
      } else {
        showToast("error", `⚠️ Training failed with status: ${response.status}`);
        setTraining(false);
      }
    } catch (err) {
      showToast("error", "❌ Failed to trigger training. Please try again.");
      setTraining(false);
    }
  };

  // ── FIX: Accept string | null | undefined ──
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

  const renderMetrics = (metrics: any) => {
    if (!metrics) return <p className="text-mist/40 text-sm">No model trained yet.</p>;
    return (
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-2">
        <MetricCard label="Accuracy" value={metrics.accuracy} />
        <MetricCard label="Precision" value={metrics.precision} />
        <MetricCard label="Recall" value={metrics.recall} />
        <MetricCard label="F1 Score" value={metrics.f1} />
        <MetricCard label="ROC AUC" value={metrics.roc_auc} />
        <MetricCard label="Train Size" value={metrics.train_size} />
        <MetricCard label="Val Size" value={metrics.val_size} />
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

      {/* Model Status */}
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
            {status?.production_model ? (
              <div>
                <div className="flex items-center gap-4 flex-wrap">
                  <span className="font-mono text-sm text-paper">
                    Version: {status.production_model.version}
                  </span>
                  <span className="font-mono text-xs text-mist/60">
                    Trained: {formatDate(status.production_model.training_date)}
                  </span>
                  <span
                    className={`font-mono text-xs px-2 py-0.5 rounded-full ${
                      status.production_model.status === "active"
                        ? "bg-signal-buy/20 text-signal-buy"
                        : "bg-signal-hold/20 text-signal-hold"
                    }`}
                  >
                    {status.production_model.status}
                  </span>
                </div>
                {renderMetrics(status.production_model.metrics)}
              </div>
            ) : (
              <p className="text-mist/40 text-sm">No production model deployed.</p>
            )}
          </div>

          {/* Candidate Model */}
          {status?.candidate_model && (
            <div className="bg-graphite border border-slate/60 rounded-xl p-5">
              <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
                🧪 Candidate Model
              </h3>
              <div>
                <div className="flex items-center gap-4 flex-wrap">
                  <span className="font-mono text-sm text-paper">
                    Version: {status.candidate_model.version}
                  </span>
                  <span className="font-mono text-xs text-mist/60">
                    Trained: {formatDate(status.candidate_model.training_date)}
                  </span>
                  <span
                    className={`font-mono text-xs px-2 py-0.5 rounded-full ${
                      status.candidate_model.status === "candidate"
                        ? "bg-signal-prepare/20 text-signal-prepare"
                        : "bg-signal-hold/20 text-signal-hold"
                    }`}
                  >
                    {status.candidate_model.status}
                  </span>
                </div>
                {renderMetrics(status.candidate_model.metrics)}
              </div>
            </div>
          )}

          {/* Overall stats */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <StatCard label="Last Training" value={formatDate(status?.last_training_date)} />
            <StatCard label="Dataset Size" value={status?.dataset_size ?? 0} />
            <StatCard label="T+1 Success" value={(status?.performance?.['T+1 Success'] ?? 0) + "%"} />
          </div>
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

function MetricCard({ label, value }: { label: string; value: number | string }) {
  const num = typeof value === "number" ? (value * 100).toFixed(1) + "%" : value;
  return (
    <div className="bg-ink/40 border border-slate/40 rounded-lg px-3 py-2">
      <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">{label}</div>
      <div className="font-mono text-sm text-paper mt-0.5">{num}</div>
    </div>
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