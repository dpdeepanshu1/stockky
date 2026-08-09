// frontend/src/components/Training.tsx

import React, { useState, useEffect } from "react";
import { api, TrainingModelStatus } from "../api";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";

const Training: React.FC = () => {
  const [status, setStatus] = useState<TrainingModelStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [trainingInProgress, setTrainingInProgress] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchStatus = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getTrainingStatus();
      setStatus(data);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const handleTrain = async () => {
    setTrainingInProgress(true);
    setError(null);
    try {
      await api.triggerTraining();
      setTimeout(fetchStatus, 3000);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setTrainingInProgress(false);
    }
  };

  useEffect(() => {
    fetchStatus();
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="font-mono text-mist">Loading training data...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="font-mono text-signal-sell">Error: {error}</p>
      </div>
    );
  }

  const t1Success = status?.performance?.['T+1 Success'] ?? 0;
  const t5Success = status?.performance?.['T+5 Success'] ?? 0;

  const prodModel = status?.production_model;
  const candModel = status?.candidate_model;

  const chartData = [
    { name: 'T+1 Success', value: t1Success },
    { name: 'T+5 Success', value: t5Success },
  ];

  return (
    <div className="container mx-auto p-6">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-3xl font-bold text-paper">Training Intelligence</h1>
        <button
          onClick={handleTrain}
          disabled={trainingInProgress}
          className="font-mono text-xs uppercase tracking-widest bg-signal-prepare/10 text-signal-prepare border border-signal-prepare/30 rounded-lg px-4 py-2 hover:bg-signal-prepare/20 transition disabled:opacity-50"
        >
          {trainingInProgress ? 'Training...' : 'Trigger Training'}
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
        <div className="bg-graphite border border-slate/60 rounded-xl p-4">
          <div className="text-sm text-mist">T+1 Success</div>
          <div className="text-2xl font-bold text-paper">{t1Success}%</div>
        </div>
        <div className="bg-graphite border border-slate/60 rounded-xl p-4">
          <div className="text-sm text-mist">T+5 Success</div>
          <div className="text-2xl font-bold text-paper">{t5Success}%</div>
        </div>
        <div className="bg-graphite border border-slate/60 rounded-xl p-4">
          <div className="text-sm text-mist">Dataset Size</div>
          <div className="text-2xl font-bold text-paper">{status?.dataset_size ?? 0}</div>
        </div>
        <div className="bg-graphite border border-slate/60 rounded-xl p-4">
          <div className="text-sm text-mist">Model Version</div>
          <div className="text-2xl font-bold text-paper">{prodModel?.version || 'None'}</div>
        </div>
      </div>

      <div className="bg-graphite border border-slate/60 rounded-xl p-4 mb-6">
        <div className="text-paper font-bold mb-4">Model Performance</div>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartData}>
              <XAxis dataKey="name" stroke="#888" fontSize={12} />
              <YAxis stroke="#888" fontSize={12} />
              <Tooltip
                contentStyle={{ backgroundColor: '#1a1a2e', border: '1px solid #333' }}
                labelStyle={{ color: '#e0e0e0' }}
              />
              <Bar dataKey="value" fill="#4f46e5" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="bg-graphite border border-slate/60 rounded-xl p-4">
          <div className="text-sm text-mist mb-2">Production Model</div>
          {prodModel ? (
            <div className="space-y-1 font-mono text-xs text-mist">
              <p>Version: <span className="text-paper">{prodModel.version}</span></p>
              <p>Trained: <span className="text-paper">
                {prodModel.training_date ? new Date(prodModel.training_date).toLocaleString() : 'Unknown'}
              </span></p>
              {prodModel.metrics ? (
                <>
                  <p>Accuracy: <span className="text-paper">{prodModel.metrics.accuracy.toFixed(2)}</span></p>
                  <p>Precision: <span className="text-paper">{prodModel.metrics.precision.toFixed(2)}</span></p>
                  <p>Recall: <span className="text-paper">{prodModel.metrics.recall.toFixed(2)}</span></p>
                  <p>F1: <span className="text-paper">{prodModel.metrics.f1.toFixed(2)}</span></p>
                  <p>ROC AUC: <span className="text-paper">{prodModel.metrics.roc_auc.toFixed(2)}</span></p>
                </>
              ) : (
                <p className="text-mist/60">Metrics not available</p>
              )}
            </div>
          ) : (
            <p className="text-mist/60 text-sm">No production model yet.</p>
          )}
        </div>

        <div className="bg-graphite border border-slate/60 rounded-xl p-4">
          <div className="text-sm text-mist mb-2">Candidate Model</div>
          {candModel ? (
            <div className="space-y-1 font-mono text-xs text-mist">
              <p>Version: <span className="text-paper">{candModel.version}</span></p>
              <p>Trained: <span className="text-paper">
                {candModel.training_date ? new Date(candModel.training_date).toLocaleString() : 'Unknown'}
              </span></p>
              {candModel.metrics ? (
                <>
                  <p>Accuracy: <span className="text-paper">{candModel.metrics.accuracy.toFixed(2)}</span></p>
                  <p>Precision: <span className="text-paper">{candModel.metrics.precision.toFixed(2)}</span></p>
                  <p>Recall: <span className="text-paper">{candModel.metrics.recall.toFixed(2)}</span></p>
                  <p>F1: <span className="text-paper">{candModel.metrics.f1.toFixed(2)}</span></p>
                  <p>ROC AUC: <span className="text-paper">{candModel.metrics.roc_auc.toFixed(2)}</span></p>
                </>
              ) : (
                <p className="text-mist/60">Metrics not available</p>
              )}
            </div>
          ) : (
            <p className="text-mist/60 text-sm">No candidate model yet. Run training to create one.</p>
          )}
        </div>
      </div>
    </div>
  );
};

export default Training;