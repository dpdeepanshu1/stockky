const API_URL = import.meta.env.VITE_API_URL || "https://api-gateway-wizr.onrender.com";

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

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body}`);
  }
  return res.json();
}

export const api = {
  getStock: (symbol: string, alreadyOwned = false) =>
    fetch(`${API_URL}/stock/${symbol}?already_owned=${alreadyOwned}`).then((r) =>
      handle<Decision>(r)
    ),
  runScan: () =>
    fetch(`${API_URL}/scan`).then((r) => handle<ScanResult>(r)),
  getWatchlist: () =>
    fetch(`${API_URL}/watchlist`).then((r) => handle<{ symbols: string[] }>(r)),
  setWatchlist: (symbols: string[]) =>
    fetch(`${API_URL}/watchlist`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols }),
    }).then((r) => handle<{ symbols: string[] }>(r)),
};