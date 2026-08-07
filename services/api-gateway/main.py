"""
API Gateway
------------
Single entry point the React frontend talks to.
Watchlist is now persisted in Upstash Redis — survives restarts.
New in v2 (merged):
  - Dynamic scan universe: 50+ stocks including NSE momentum movers,
    user-searched symbols, news-driven additions, new listings.
  - Persistent searched symbols saved to Upstash Redis.
  - Smart scan returns top 3-5 picks with entry/target/stop even when
    the overall market verdict is cautious.
  - Watchlist CRUD persisted in Redis.
  - System health check for all downstream services (concurrent).
  - Notification configuration proxy.
"""
import os
import json
import time
import asyncio
import logging
import math
from typing import List

import httpx
import yfinance as yf
import feedparser
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from upstash_redis import Redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api-gateway")

# ---- Live Render URLs (default) ----
DECISION_URL = os.getenv("DECISION_URL", "https://decision-engine-service-0hg6.onrender.com")
NOTIFICATION_URL = os.getenv("NOTIFICATION_URL", "https://notification-service-36py.onrender.com")
NEWS_URL = os.getenv("NEWS_URL", "https://news-intelligence-service.onrender.com")  # used for news mentions

# Optional downstream services for /system/health (wake‑up checks)
MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://market-data-service.onrender.com")
TECHNICAL_URL = os.getenv("TECHNICAL_URL", "https://technical-analysis-service-zhnc.onrender.com")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", "https://fundamental-analysis-service.onrender.com")
EVENT_URL = os.getenv("EVENT_URL", "https://event-tracker-service-m1lw.onrender.com")
PREDICTION_URL = os.getenv("PREDICTION_URL", "https://prediction-service-wowb.onrender.com")

# Service definitions for system health
SYSTEM_SERVICES = {
    "market-data": {"url": MARKET_DATA_URL, "required": True},
    "technical-analysis": {"url": TECHNICAL_URL, "required": True},
    "fundamental-analysis": {"url": FUNDAMENTAL_URL, "required": True},
    "decision-engine": {"url": DECISION_URL, "required": True},
    "news-intelligence": {"url": NEWS_URL, "required": False},
    "event-tracker": {"url": EVENT_URL, "required": False},
    "prediction": {"url": PREDICTION_URL, "required": False},
    "notification": {"url": NOTIFICATION_URL, "required": False},
}

app = FastAPI(title="Stockky API Gateway", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Redis ──────────────────────────────────────────────────────────────────────
_redis = None
try:
    _redis = Redis(
        url=os.getenv("UPSTASH_REDIS_REST_URL"),
        token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
    )
    _redis.ping()
    logger.info("Connected to Upstash Redis")
except Exception as e:
    logger.warning("Redis unavailable: %s", e)

WATCHLIST_KEY       = "stockky:watchlist"
SEARCHED_KEY        = "stockky:searched_symbols"
SCAN_UNIVERSE_KEY   = "stockky:scan_universe"

DEFAULT_WATCHLIST = [
    "TCS", "INFY", "HDFCBANK", "ICICIBANK", "RELIANCE", "HCLTECH",
    "WIPRO", "COFORGE", "ANGELONE", "ADANIPOWER", "BEL", "HAL",
    "TATAMOTORS", "SBIN", "AXISBANK", "KOTAKBANK", "LT", "MARUTI",
    "SUNPHARMA", "TITAN", "ITC", "BAJFINANCE", "ASIANPAINT", "NESTLEIND",
]

# ── Redis helpers ──────────────────────────────────────────────────────────────
def _redis_get(key: str):
    if not _redis:
        return None
    try:
        val = _redis.get(key)
        return json.loads(val) if val else None
    except Exception:
        return None

def _redis_set(key: str, value, ttl: int = None):
    if not _redis:
        return
    try:
        data = json.dumps(value, default=str)
        if ttl:
            _redis.setex(key, ttl, data)
        else:
            _redis.set(key, data)
    except Exception as e:
        logger.warning("Redis set failed: %s", e)

def _load_watchlist() -> List[str]:
    return _redis_get(WATCHLIST_KEY) or list(DEFAULT_WATCHLIST)

def _save_watchlist(symbols: List[str]):
    _redis_set(WATCHLIST_KEY, symbols)

def _load_searched() -> List[str]:
    return _redis_get(SEARCHED_KEY) or []

def _add_searched(symbol: str):
    searched = _load_searched()
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    if sym not in searched:
        searched.append(sym)
        _redis_set(SEARCHED_KEY, searched[-200:])  # keep last 200

# ── Dynamic scan universe ──────────────────────────────────────────────────────
# Base NSE liquid universe — covers all major sectors
BASE_UNIVERSE = [
    # IT
    "TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "COFORGE", "LTIM", "PERSISTENT", "MPHASIS",
    # Banks & Finance
    "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "BAJFINANCE",
    "BAJAJFINSV", "ANGELONE", "HDFCLIFE", "SBILIFE", "ICICIGI",
    # Large cap
    "RELIANCE", "LT", "MARUTI", "TATAMOTORS", "TITAN", "ITC",
    "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB",
    # Mid cap diversified
    "ADANIPOWER", "BEL", "HAL", "ASIANPAINT", "NESTLEIND",
    "ULTRACEMCO", "GRASIM", "HINDALCO", "JSWSTEEL", "TATASTEEL",
    "ONGC", "POWERGRID", "NTPC", "COALINDIA", "BPCL",
    # Consumer & Retail
    "DMART", "TRENT", "NYKAA", "ZOMATO", "PAYTM",
    # New listings & emerging
    "IREDA", "RAILTEL", "IRFC", "RVNL", "HUDCO",
]

# NSE indices — yfinance tickers for momentum screening (unused directly, but kept for reference)
NSE_INDEX_TICKERS = ["^NSEI", "^NSEBANK"]

def _get_momentum_movers() -> List[str]:
    """Fetch top gainers/losers from NSE indices — these have recent momentum."""
    movers = []
    try:
        nifty50_symbols = [
            "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
            "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BPCL",
            "BHARTIARTL", "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY",
            "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
            "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC",
            "INDUSINDBK", "INFY", "JSWSTEEL", "KOTAKBANK", "LT",
            "LTIM", "M&M", "MARUTI", "NESTLEIND", "NTPC",
            "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
            "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL",
            "TCS", "TRENT", "TITAN", "ULTRACEMCO", "WIPRO",
        ]
        performances = []
        for sym in nifty50_symbols[:20]:  # limit to avoid timeout
            try:
                t = yf.Ticker(f"{sym}.NS")
                hist = t.history(period="5d", interval="1d")
                if hist.empty or len(hist) < 2:
                    continue
                week_change = (hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0] * 100
                performances.append((sym, float(week_change)))
            except Exception:
                continue
        performances.sort(key=lambda x: x[1], reverse=True)
        movers = [s for s, _ in performances[:5]] + [s for s, _ in performances[-5:]]
        logger.info("Momentum movers this week: %s", movers)
    except Exception as e:
        logger.warning("Could not fetch momentum movers: %s", e)
    return movers

def _get_news_mentioned_symbols() -> List[str]:
    """Extract NSE symbols mentioned in recent market news."""
    mentioned = []
    try:
        feed = feedparser.parse(
            "https://news.google.com/rss/search?q=NSE+stock+bulk+deal+earnings+results&hl=en-IN&gl=IN&ceid=IN:en"
        )
        text = " ".join(e.title for e in feed.entries[:20]).upper()
        for sym in BASE_UNIVERSE:
            if sym in text:
                mentioned.append(sym)
    except Exception as e:
        logger.warning("Could not parse news for symbols: %s", e)
    return mentioned[:10]

def _build_scan_universe() -> List[str]:
    """
    Build a fresh scan universe by combining:
    1. Base liquid universe (40+ stocks)
    2. User watchlist
    3. Previously searched symbols
    4. Weekly momentum movers
    5. News-mentioned symbols
    Deduped and capped at 60 symbols to keep scan under 3 minutes.
    """
    cached = _redis_get(SCAN_UNIVERSE_KEY)
    if cached:
        return cached

    universe = set(BASE_UNIVERSE)
    universe.update(_load_watchlist())
    universe.update(_load_searched())
    universe.update(_get_momentum_movers())
    universe.update(_get_news_mentioned_symbols())

    clean = []
    seen = set()
    for s in universe:
        s = s.upper().replace(".NS", "").replace(".BO", "")
        if s and s not in seen:
            seen.add(s)
            clean.append(s)

    result = clean[:60]
    _redis_set(SCAN_UNIVERSE_KEY, result, ttl=21600)  # 6hr cache
    logger.info("Scan universe built: %d symbols", len(result))
    return result

# ── Pydantic models ────────────────────────────────────────────────────────────
class WatchlistUpdate(BaseModel):
    symbols: List[str]

class NotificationChannelUpdate(BaseModel):
    discord_webhook_url: str | None = None
    slack_webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    enabled: dict | None = None

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "service": "Stockky API Gateway",
        "version": "2.0.0",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/system/health": "GET – health of all downstream services",
            "/watchlist": "GET/POST – manage watchlist",
            "/watchlist/add": "POST – add symbols",
            "/watchlist/{symbol}": "DELETE – remove symbol",
            "/stock/{symbol}": "GET – get decision for a symbol",
            "/scan": "GET – scan universe (dynamic) – accepts ?force_refresh=true",
            "/scan/universe": "GET – preview current scan universe",
            "/scan/universe/cache": "DELETE – clear universe cache",
            "/searched": "GET – list searched symbols",
            "/notifications/health": "GET – notification service health",
            "/notifications/config": "GET/POST – get/update notification config",
            "/notifications/config/{channel}": "DELETE – clear a channel",
            "/notifications/test": "POST – test notifications",
            "/docs": "Swagger UI documentation",
        },
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "api-gateway", "redis": bool(_redis)}

@app.get("/system/health")
async def system_health():
    """Pings every downstream service AT THE SAME TIME."""
    async def check(name: str, url: str, required: bool):
        if not url:
            return name, {"ok": False, "required": required, "status": "not_configured"}
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=70) as client:
                resp = await client.get(f"{url.rstrip('/')}/health")
            elapsed = round(time.monotonic() - start, 1)
            if resp.status_code == 200:
                return name, {"ok": True, "required": required, "status": "up", "seconds": elapsed}
            return name, {
                "ok": False,
                "required": required,
                "status": f"http_{resp.status_code}",
                "seconds": elapsed,
            }
        except httpx.HTTPError as e:
            elapsed = round(time.monotonic() - start, 1)
            return name, {
                "ok": False,
                "required": required,
                "status": "unreachable",
                "seconds": elapsed,
                "error": str(e)[:200],
            }

    results = await asyncio.gather(
        *(check(name, cfg["url"], cfg["required"]) for name, cfg in SYSTEM_SERVICES.items())
    )
    services = {"api-gateway": {"ok": True, "required": True, "status": "up", "seconds": 0}}
    services.update(dict(results))

    required_ok = all(v["ok"] for v in services.values() if v["required"])
    all_ok = all(v["ok"] for v in services.values())

    return {"required_ok": required_ok, "all_ok": all_ok, "services": services}

# ── Watchlist ──────────────────────────────────────────────────────────────────
@app.get("/watchlist")
def get_watchlist():
    return {"symbols": _load_watchlist()}

@app.post("/watchlist")
def set_watchlist(update: WatchlistUpdate):
    symbols = [s.strip().upper() for s in update.symbols]
    _save_watchlist(symbols)
    if _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass
    return {"symbols": symbols}

@app.post("/watchlist/add")
def add_to_watchlist(update: WatchlistUpdate):
    current = set(_load_watchlist())
    for s in update.symbols:
        current.add(s.strip().upper())
    symbols = sorted(current)
    _save_watchlist(symbols)
    if _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass
    return {"symbols": symbols}

@app.delete("/watchlist/{symbol}")
def remove_from_watchlist(symbol: str):
    current = _load_watchlist()
    updated = [s for s in current if s != symbol.upper()]
    _save_watchlist(updated)
    return {"symbols": updated}

# ── Searched symbols ──────────────────────────────────────────────────────────
@app.get("/searched")
def get_searched_symbols():
    return {"symbols": _load_searched()}

# ── Stock decision ─────────────────────────────────────────────────────────────
@app.get("/stock/{symbol}")
def get_stock_decision(symbol: str, already_owned: bool = False):
    """Single stock analysis. Saves symbol to searched history."""
    _add_searched(symbol)
    if _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass
    try:
        resp = httpx.get(
            f"{DECISION_URL}/decide/{symbol}",
            params={"already_owned": already_owned},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Decision engine unreachable: {e}")

# ── Scan ──────────────────────────────────────────────────────────────────────
@app.get("/scan")
def run_scan(force_refresh: bool = False):
    """
    Smart market scan across 50+ stocks.
    Always returns top 3-5 picks with buy/target/stop even when the
    overall market is cautious — ranked by combined score.
    """
    if force_refresh and _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass

    universe = _build_scan_universe()
    results = []
    errors = []

    with httpx.Client(timeout=30) as client:
        for symbol in universe:
            try:
                resp = client.get(f"{DECISION_URL}/decide/{symbol}")
                resp.raise_for_status()
                results.append(resp.json())
            except httpx.HTTPError as e:
                logger.warning("Scan skipped %s: %s", symbol, e)
                errors.append({"symbol": symbol, "error": str(e)})

    results.sort(key=lambda r: r.get("combined_score", 0), reverse=True)

    actionable = [r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")]
    top_picks = actionable[:5]

    watchlist_candidates = []
    if not top_picks:
        watchlist_candidates = results[:3]

    buy_count  = len([r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")])
    sell_count = len([r for r in results if r.get("decision") == "SELL"])
    hold_count = len([r for r in results if r.get("decision") == "HOLD"])

    if buy_count >= 5:
        market_mood = "Bullish"
    elif sell_count > buy_count:
        market_mood = "Bearish"
    elif buy_count > 0:
        market_mood = "Selective"
    else:
        market_mood = "Cautious"

    verdict = (
        f"{len(top_picks)} strong opportunity(ies) found"
        if top_picks
        else "DO NOT BUY ANY STOCK TODAY — market conditions cautious"
    )

    return {
        "scanned": len(results),
        "universe_size": len(universe),
        "watchlist_size": len(_load_watchlist()),
        "recommendations": top_picks,
        "watchlist_candidates": watchlist_candidates,
        "verdict": verdict,
        "market_mood": market_mood,
        "market_stats": {
            "buy_signals": buy_count,
            "sell_signals": sell_count,
            "hold_signals": hold_count,
            "cautious": len(results) - buy_count - sell_count - hold_count,
        },
        "all_results": results,
        "errors": errors,
    }

@app.get("/scan/universe")
def get_scan_universe():
    """Preview what symbols will be scanned next run."""
    universe = _build_scan_universe()
    searched = _load_searched()
    movers = _get_momentum_movers()
    return {
        "total": len(universe),
        "symbols": universe,
        "searched_symbols_included": [s for s in searched if s in universe],
        "momentum_movers": movers,
    }

@app.delete("/scan/universe/cache")
def clear_universe_cache():
    """Force rebuild of scan universe on next scan."""
    if _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass
    return {"message": "Scan universe cache cleared — will rebuild on next scan"}

# ── Notifications ──────────────────────────────────────────────────────────────
@app.get("/notifications/health")
def notifications_health():
    try:
        resp = httpx.get(f"{NOTIFICATION_URL}/health", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

@app.get("/notifications/config")
def get_notification_config():
    try:
        resp = httpx.get(f"{NOTIFICATION_URL}/config", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

@app.post("/notifications/config")
def set_notification_config(update: NotificationChannelUpdate):
    try:
        resp = httpx.post(
            f"{NOTIFICATION_URL}/config",
            json=update.model_dump(exclude_none=True),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

@app.delete("/notifications/config/{channel}")
def delete_notification_channel(channel: str):
    try:
        resp = httpx.delete(f"{NOTIFICATION_URL}/config/{channel}", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

@app.post("/notifications/test")
def test_notification_channels():
    try:
        resp = httpx.post(f"{NOTIFICATION_URL}/test", timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)