const STORAGE_KEY = "stockky:api_url";

/**
 * Vite bakes VITE_API_URL into the static bundle at BUILD time. If the
 * frontend is deployed without that build-time env var set (a common
 * mistake when frontend + backend are deployed separately), every fetch
 * silently targets http://localhost:8000 from the visitor's browser --
 * which fails instantly with "Failed to fetch". To make the app resilient
 * to that misconfiguration without a rebuild, the backend URL can also be
 * set at runtime from the Settings banner and is remembered in
 * localStorage, taking priority over the build-time value.
 */
export function getApiUrl(): string {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored) return stored;
  return (import.meta.env.VITE_API_URL || "").replace(/\/$/, "");
}

export function setApiUrl(url: string) {
  const clean = url.trim().replace(/\/$/, "");
  if (clean) localStorage.setItem(STORAGE_KEY, clean);
  else localStorage.removeItem(STORAGE_KEY);
}

export function clearApiUrlOverride() {
  localStorage.removeItem(STORAGE_KEY);
}

export interface Decision {
  symbol: string;
  decision: "BUY NOW" | "PREPARE TO BUY" | "HOLD" | "DO NOT BUY" | "SELL";
  confidence: "High" | "Medium" | "Low";
  combined_score: number;
  technical_score: number;
  fundamental_score: number;
  news_score: number | null;
  prediction_score: number | null;
  event_risk: boolean;
  entry_range: { low: number; high: number };
  target: number;
  stop_loss: number;
  holding_period: string;
  close: number;
  support: number;
  resistance: number;
  reasons: {
    technical: string[];
    fundamental: string[];
    news?: string[];
    prediction?: string[];
    event?: string[];
  };
  valuation: string;
  sector: string | null;
}

export interface ScanResult {
  scanned: number;
  watchlist_size: number;
  recommendations: Decision[];
  verdict: string;
  all_results: Decision[];
  errors: { symbol: string; error: string }[];
}

export interface NotificationChannelStatus {
  configured: boolean;
  enabled: boolean;
  masked: string;
  chat_id?: string;
}

export interface NotificationConfig {
  discord: NotificationChannelStatus;
  slack: NotificationChannelStatus;
  telegram: NotificationChannelStatus;
  persisted: boolean;
}

/** Central fetch wrapper: turns the raw browser "Failed to fetch" TypeError
 * into an actionable message, and surfaces backend error bodies cleanly. */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const base = getApiUrl();
  if (!base) {
    throw new Error(
      "Backend URL isn't set. Open Settings (top right) and paste your API Gateway URL."
    );
  }

  let res: Response;
  try {
    res = await fetch(`${base}${path}`, init);
  } catch {
    throw new Error(
      `Could not reach ${base}. The backend may be asleep (free-tier cold start -- retry in ~30s), down, or the URL is wrong. Check Settings.`
    );
  }

  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${body ? `: ${body.slice(0, 240)}` : ""}`);
  }

  return res.json();
}

export const api = {
  ping: () => request<{ status: string; service: string }>("/health"),

  getStock: (symbol: string, alreadyOwned = false) =>
    request<Decision>(`/stock/${symbol}?already_owned=${alreadyOwned}`),

  runScan: () => request<ScanResult>("/scan"),

  getWatchlist: () => request<{ symbols: string[] }>("/watchlist"),

  setWatchlist: (symbols: string[]) =>
    request<{ symbols: string[] }>("/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols }),
    }),

  getNotificationConfig: () => request<NotificationConfig>("/notifications/config"),

  saveNotificationConfig: (update: {
    discord_webhook_url?: string;
    slack_webhook_url?: string;
    telegram_bot_token?: string;
    telegram_chat_id?: string;
    enabled?: Partial<Record<"discord" | "slack" | "telegram", boolean>>;
  }) =>
    request<NotificationConfig>("/notifications/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(update),
    }),

  clearNotificationChannel: (channel: "discord" | "slack" | "telegram") =>
    request<NotificationConfig>(`/notifications/config/${channel}`, { method: "DELETE" }),

  testNotifications: () =>
    request<{ delivered: boolean; note?: string; results?: Record<string, string> }>(
      "/notifications/test",
      { method: "POST" }
    ),
};