import { useEffect, useState } from "react";
import { api, wakeService, SystemServiceStatus } from "../api";

interface ServiceManagerProps {
  onClose: () => void;
}

export default function ServiceManager({ onClose }: ServiceManagerProps) {
  const [services, setServices] = useState<Record<string, SystemServiceStatus>>({});
  const [loading, setLoading] = useState(true);
  const [waking, setWaking] = useState<Record<string, boolean>>({});

  const fetchServices = async () => {
    setLoading(true);
    try {
      const health = await api.systemHealth();
      setServices(health.services);
    } catch (error) {
      console.error("Failed to fetch services", error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchServices();
  }, []);

  const handleWake = async (name: string, url: string | null | undefined) => {
    if (!url) return;
    setWaking((prev) => ({ ...prev, [name]: true }));
    try {
      // Open the render URL in a small popup to trigger wake
      const popup = window.open(url + "/health", "_blank", "width=400,height=200");
      setTimeout(() => {
        if (popup) popup.close();
      }, 3000);
      // Wait a bit and then re-fetch status
      setTimeout(async () => {
        await fetchServices();
        setWaking((prev) => ({ ...prev, [name]: false }));
      }, 5000);
    } catch {
      setWaking((prev) => ({ ...prev, [name]: false }));
    }
  };

  const handleWakeAll = () => {
    for (const [name, status] of Object.entries(services)) {
      if (status.url) {
        handleWake(name, status.url);
      }
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/80 backdrop-blur-sm p-4">
      <div className="bg-graphite border border-slate/60 rounded-2xl max-w-2xl w-full max-h-[80vh] overflow-y-auto p-6 shadow-2xl">
        <div className="flex items-center justify-between mb-6">
          <h2 className="font-display text-2xl text-paper">🛠️ Service Manager</h2>
          <button onClick={onClose} className="font-mono text-xs text-mist hover:text-paper transition">
            ✕ Close
          </button>
        </div>

        {loading ? (
          <div className="text-center py-8 text-mist/40 font-mono">Loading services...</div>
        ) : (
          <>
            <div className="flex justify-end mb-4">
              <button
                onClick={handleWakeAll}
                className="font-mono text-xs border border-signal-prepare/40 text-signal-prepare px-4 py-1.5 rounded-lg hover:bg-signal-prepare/10 transition"
              >
                Wake All Services
              </button>
            </div>
            <div className="space-y-2">
              {Object.entries(services).map(([name, status]) => {
                const isWaking = waking[name] || false;
                return (
                  <div
                    key={name}
                    className="flex items-center justify-between border border-slate/40 rounded-lg px-4 py-3 bg-ink/60"
                  >
                    <div>
                      <span className="font-mono text-sm text-paper">{name}</span>
                      <span className="ml-2 font-mono text-[10px] text-mist/50">
                        {status.required ? "required" : "optional"}
                      </span>
                      <div className="flex items-center gap-2 mt-1">
                        <span
                          className={`font-mono text-xs ${
                            status.ok ? "text-signal-buy" : "text-signal-sell"
                          }`}
                        >
                          {status.ok ? "✅ Online" : "❌ Offline"}
                        </span>
                        {status.seconds && (
                          <span className="font-mono text-[10px] text-mist/40">
                            {status.seconds}s
                          </span>
                        )}
                      </div>
                    </div>
                    {status.url && (
                      <button
                        onClick={() => handleWake(name, status.url)}
                        disabled={isWaking}
                        className="font-mono text-xs px-3 py-1.5 border border-slate rounded hover:border-mist hover:text-paper transition disabled:opacity-50"
                      >
                        {isWaking ? "Waking..." : "Wake"}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
            <div className="mt-4 text-xs text-mist/40 font-mono">
              Click "Wake" to open the service's health endpoint in a small window, triggering a cold start.
            </div>
          </>
        )}
      </div>
    </div>
  );
}