import { useEffect, useRef, useState } from "react";
import { api, getApiUrl, setApiUrl, SystemHealth } from "../api";

type Stage =
  | { phase: "checking-gateway" }
  | { phase: "gateway-down" }
  | { phase: "waking"; health: SystemHealth | null; attempt: number }
  | { phase: "ready" };

const SERVICE_LABELS: Record<string, string> = {
  "api-gateway": "API Gateway",
  "market-data": "Market Data",
  "technical-analysis": "Technical Analysis",
  "fundamental-analysis": "Fundamental Analysis",
  "decision-engine": "Decision Engine",
  "news-intelligence": "News Intelligence",
  "event-tracker": "Event Tracker",
  prediction: "Prediction Model",
  notification: "Notifications",
};

const MAX_AUTO_ATTEMPTS = 6; // ~ up to a couple minutes total, on top of each call's own internal wait
const ESCAPE_HATCH_AFTER_ATTEMPTS = 3;

export default function SystemCheck({ onReady }: { onReady: () => void }) {
  const [stage, setStage] = useState<Stage>({ phase: "checking-gateway" });
  const [apiUrlInput, setApiUrlInput] = useState(getApiUrl());
  const cancelled = useRef(false);

  useEffect(() => {
    cancelled.current = false;
    runCheck(0);
    return () => {
      cancelled.current = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function runCheck(attempt: number) {
    try {
      await api.ping(); // wakes + confirms api-gateway itself
    } catch {
      if (!cancelled.current) setStage({ phase: "gateway-down" });
      return;
    }

    try {
      const health = await api.systemHealth(); // wakes + checks every downstream service concurrently
      if (cancelled.current) return;

      if (health.required_ok) {
        setStage({ phase: "ready" });
        setTimeout(() => {
          if (!cancelled.current) onReady();
        }, 700);
        return;
      }

      setStage({ phase: "waking", health, attempt });
      if (attempt < MAX_AUTO_ATTEMPTS) {
        setTimeout(() => runCheck(attempt + 1), 5000);
      }
    } catch (e) {
      if (!cancelled.current) {
        setStage({
          phase: "waking",
          health: null,
          attempt,
        });
        if (attempt < MAX_AUTO_ATTEMPTS) {
          setTimeout(() => runCheck(attempt + 1), 5000);
        }
      }
    }
  }

  function saveGatewayUrl() {
    setApiUrl(apiUrlInput);
    setStage({ phase: "checking-gateway" });
    runCheck(0);
  }

  if (stage.phase === "gateway-down") {
    return (
      <GateShell>
        <p className="font-mono text-xs text-signal-sell uppercase tracking-widest mb-2">
          Can't reach the backend
        </p>
        <p className="text-mist text-sm mb-4 max-w-sm">
          Set your API Gateway URL to continue.
        </p>
        <div className="flex gap-2 max-w-md">
          <input
            value={apiUrlInput}
            onChange={(e) => setApiUrlInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && saveGatewayUrl()}
            placeholder="https://your-api-gateway.onrender.com"
            className="flex-1 bg-ink/60 border border-slate rounded-lg px-3 py-2 font-mono text-xs text-paper placeholder:text-mist/30 outline-none focus:border-signal-prepare/60 transition"
            spellCheck={false}
            autoComplete="off"
          />
          <button
            onClick={saveGatewayUrl}
            className="border border-slate rounded-lg px-4 py-2 font-mono text-xs text-mist hover:text-paper hover:border-signal-prepare/60 transition shrink-0"
          >
            Connect
          </button>
        </div>
      </GateShell>
    );
  }

  if (stage.phase === "checking-gateway") {
    return (
      <GateShell>
        <p className="font-mono text-xs text-mist uppercase tracking-widest">
          Connecting to backend...
        </p>
      </GateShell>
    );
  }

  if (stage.phase === "ready") {
    return (
      <GateShell>
        <p className="font-mono text-sm text-signal-buy uppercase tracking-widest">
          All Services Connected Successfully
        </p>
      </GateShell>
    );
  }

  // phase === "waking"
  const services = stage.health?.services || {};
  const entries = Object.entries(services);
  const showEscapeHatch = stage.attempt >= ESCAPE_HATCH_AFTER_ATTEMPTS;

  return (
    <GateShell>
      <p className="font-mono text-xs text-mist uppercase tracking-widest mb-1">
        Waking up services
      </p>
      <p className="text-mist/60 text-xs mb-6 max-w-sm">
        Everything runs on free-tier hosting, so a sleeping service can take up to a minute to
        wake on its first request. This only happens after a period of no activity.
      </p>

      <div className="space-y-1.5 mb-6 w-full max-w-sm">
        {entries.length === 0 && (
          <p className="font-mono text-[11px] text-mist/40">Checking...</p>
        )}
        {entries.map(([name, s]) => (
          <ServiceRow key={name} name={SERVICE_LABELS[name] || name} status={s} />
        ))}
      </div>

      <div className="flex items-center gap-4">
        <button
          onClick={() => runCheck(stage.attempt + 1)}
          className="font-mono text-xs text-mist hover:text-paper border border-slate rounded-lg px-3 py-2 hover:border-mist/60 transition"
        >
          Recheck now
        </button>
        {showEscapeHatch && (
          <button
            onClick={onReady}
            className="font-mono text-xs text-mist/50 hover:text-paper underline"
          >
            Continue anyway
          </button>
        )}
      </div>
    </GateShell>
  );
}

function ServiceRow({ name, status }: { name: string; status: { ok: boolean; required: boolean; status: string } }) {
  const icon = status.ok ? (
    <span className="text-signal-buy">up</span>
  ) : status.status === "not_configured" ? (
    <span className="text-mist/30">--</span>
  ) : (
    <span className="text-signal-hold animate-pulse">waking</span>
  );
  return (
    <div className="flex items-center justify-between font-mono text-[11px] border-b border-slate/30 py-1.5">
      <span className={status.required ? "text-mist" : "text-mist/50"}>
        {name}
        {!status.required && <span className="text-mist/30"> (optional)</span>}
      </span>
      {icon}
    </div>
  );
}

function GateShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-ink text-paper flex flex-col items-center justify-center px-4">
      <span className="font-display text-xl tracking-tight mb-8">Stockky</span>
      <div className="flex flex-col items-center text-center">{children}</div>
    </div>
  );
}
