const STORAGE_KEY = "stockky:api_url";

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
  natural_language_summary: string;
}

export interface ScanResult {
  scanned: number;
  universe_size: number;
  watchlist_size: number;
  recommendations: Decision[];
  watchlist_candidates: Decision[];
  verdict: string;
  market_mood: string;
  market_stats: {
    buy_signals: number;
    sell_signals: number;
    hold_signals: number;
    cautious: number;
  };
  all_results: Decision[];
  errors: { symbol: string; error: string }[];
}

export interface ScanStatus {
  status: "running" | "done" | "error";
  total: number;
  processed: number;
  elapsed: number;
  estimated_remaining?: number | null;
  result?: ScanResult;
  error?: string;
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

export interface SystemServiceStatus {
  ok: boolean;
  required: boolean;
  status: string;
  seconds?: number;
  error?: string;
  url?: string | null;
}

export interface SystemHealth {
  required_ok: boolean;
  all_ok: boolean;
  services: Record<string, SystemServiceStatus>;
}

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

  systemHealth: () => request<SystemHealth>("/system/health"),

  getStock: (symbol: string, alreadyOwned = false) =>
    request<Decision>(`/stock/${symbol}?already_owned=${alreadyOwned}`),

  // Synchronous scan (legacy)
  runScan: () => request<ScanResult>("/scan"),

  // Asynchronous scan with progress
  scanStart: (forceRefresh = false) =>
    request<{ task_id: string }>(`/scan/start?force_refresh=${forceRefresh}`, { method: "POST" }),

  scanStatus: (taskId: string) =>
    request<ScanStatus>(`/scan/status/${taskId}`),

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