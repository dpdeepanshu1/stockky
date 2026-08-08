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

export interface FundamentalMetrics {
  revenue_growth: number | null;
  earnings_growth: number | null;
  roe: number | null;
  debt_to_equity: number | null;
  free_cashflow: number | null;
  profit_margins: number | null;
  institutional_holding: number | null;
  pe_ratio: number | null;
  forward_pe: number | null;
}

export interface Decision {
  symbol: string;
  decision: "BUY NOW" | "PREPARE TO BUY" | "HOLD" | "DO NOT BUY" | "SELL" | "WAIT";
  confidence: "High" | "Medium" | "Low";
  combined_score: number;
  technical_score: number;
  fundamental_score: number;
  news_score: number | null;
  prediction_score: number | null;
  event_risk: boolean;
  entry_range: { low: number | null; high: number | null } | null;
  target: number | null;
  stop_loss: number | null;
  holding_period: string;
  close: number | null;
  support: number | null;
  resistance: number | null;
  reasons: {
    technical: string[];
    fundamental: string[];
    news?: string[];
    prediction?: string[];
    event?: string[];
  };
  valuation: string;
  sector: string | null;
  natural_language_summary?: string;
  fundamental_metrics?: FundamentalMetrics;
  data_insufficient?: boolean;
  fundamental_fallback?: boolean; // <--- NEW: For showing fallback messaging
  event_score_delta?: number;
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

export interface MarketStock {
  symbol: string;
  price: number;
  change: number;
  change_pct: number;
  volume?: number;
  high?: number;
  low?: number;
}

export interface MarketResponse {
  data: MarketStock[];
  count: number;
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

async function request<T>(path: string, init?: RequestInit, retries = 2, timeoutMs = 60000): Promise<T> {
  const base = getApiUrl();
  if (!base) {
    throw new Error(
      "Backend URL isn't set. Open Settings (top right) and paste your API Gateway URL."
    );
  }

  const url = `${base}${path}`;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, {
      ...init,
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(`${response.status} ${response.statusText}${body ? `: ${body.slice(0, 240)}` : ""}`);
    }

    return response.json();
  } catch (error) {
    clearTimeout(timeoutId);

    if (retries > 0 && (error instanceof TypeError || error instanceof DOMException)) {
      await new Promise((resolve) => setTimeout(resolve, 1000 * (3 - retries)));
      return request<T>(path, init, retries - 1, timeoutMs);
    }

    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error(
        `Request timed out after ${timeoutMs / 1000} seconds. The backend may be waking up (free-tier cold start). Try again in a moment.`
      );
    }

    throw error;
  }
}

export async function wakeService(url: string): Promise<void> {
  if (!url) return;
  try {
    await fetch(url + "/health", { mode: "no-cors" });
  } catch {
    // Ignore – request still wakes the service
  }
}

export const api = {
  ping: () => request<{ status: string; service: string }>("/health", undefined, 2, 30000),

  systemHealth: () => request<SystemHealth>("/system/health", undefined, 2, 60000),

  getStock: (symbol: string, alreadyOwned = false) =>
    request<Decision>(`/stock/${symbol}?already_owned=${alreadyOwned}`, undefined, 2, 60000),

  runScan: () => request<ScanResult>("/scan", undefined, 2, 120000),

  scanStart: (forceRefresh = false) =>
    request<{ task_id: string }>(
      `/scan/start?force_refresh=${forceRefresh}`,
      { method: "POST" },
      2,
      120000
    ),

  scanStatus: (taskId: string) =>
    request<ScanStatus>(`/scan/status/${taskId}`, undefined, 2, 10000),

  scanWatchlist: () =>
    request<ScanResult>("/scan/watchlist", undefined, 2, 120000),

  getWatchlist: () => request<{ symbols: string[] }>("/watchlist", undefined, 2, 30000),

  setWatchlist: (symbols: string[]) =>
    request<{ symbols: string[] }>(
      "/watchlist",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols }),
      },
      2,
      30000
    ),

  addToWatchlist: (symbol: string) =>
    request<{ symbols: string[] }>(
      "/watchlist/add",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: [symbol] }),
      },
      2,
      30000
    ),

  marketTopGainers: () => request<MarketResponse>("/market/top-gainers", undefined, 2, 30000),
  marketTopLosers: () => request<MarketResponse>("/market/top-losers", undefined, 2, 30000),
  marketMostActive: () => request<MarketResponse>("/market/most-active", undefined, 2, 30000),
  marketTrending: () => request<MarketResponse>("/market/trending", undefined, 2, 30000),

  getNotificationConfig: () => request<NotificationConfig>("/notifications/config", undefined, 2, 30000),

  saveNotificationConfig: (update: {
    discord_webhook_url?: string;
    slack_webhook_url?: string;
    telegram_bot_token?: string;
    telegram_chat_id?: string;
    enabled?: Partial<Record<"discord" | "slack" | "telegram", boolean>>;
  }) =>
    request<NotificationConfig>(
      "/notifications/config",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(update),
      },
      2,
      30000
    ),

  clearNotificationChannel: (channel: "discord" | "slack" | "telegram") =>
    request<NotificationConfig>(`/notifications/config/${channel}`, { method: "DELETE" }, 2, 30000),

  testNotifications: () =>
    request<{ delivered: boolean; note?: string; results?: Record<string, string> }>(
      "/notifications/test",
      { method: "POST" },
      2,
      30000
    ),

  sendPicksToTelegram: (payload: { type: string; recommendations: Decision[] }) =>
    request<{ success: boolean; sent: number; message: string }>(
      "/notifications/send-picks",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
      2,
      30000
    ),
};