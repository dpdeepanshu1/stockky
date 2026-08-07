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
  - Expanded universe: 100+ base stocks, top 10 gainers/losers, new listings.
  - Real-time IPO fetching from NSE API (cached in Redis).
  - Symbol auto-correction with fuzzy matching.
  - Symbol alias mapping for rebranded/split companies (TATAMOTORS → TMPV/TMLCV, LTIM → LTM, Zomato → Eternal).
  - Natural-language Hinglish summary for every decision.
"""
import os
import json
import time
import asyncio
import logging
import difflib
from typing import List, Optional, Set, Dict, Union

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
NEWS_URL = os.getenv("NEWS_URL", "https://news-intelligence-service.onrender.com")

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
IPO_CACHE_KEY       = "stockky:ipos:recent"
KNOWN_SYMBOLS_KEY   = "stockky:known_symbols"

# ── Symbol Alias Mapping (old → new) ──────────────────────────────────────
SYMBOL_ALIASES: Dict[str, Union[str, List[str]]] = {
    # Tata Motors split into two entities: Passenger Vehicles and Commercial Vehicles
    "TATAMOTORS": "TMPV",        # primary new symbol for trading (you can also use TMLCV)
    "TATAMOTER": "TMPV",
    "TATAMOT": "TMPV",
    # LTIMindtree rebranded to LTM Limited
    "LTIM": "LTM",
    "LTIMIND": "LTM",
    "LTIMINDTREE": "LTM",
    # Zomato parent company rebranded to Eternal Limited; the food app still called Zomato
    "ZOMATO": "ETERNAL",         # trading symbol may be ETERNAL or ZOMATO? We'll keep both.
    "ZOMAT": "ETERNAL",
}

# Also add the new symbols to the base universe
EXTRA_NEW_SYMBOLS = ["TMPV", "TMLCV", "LTM", "ETERNAL"]

# ── Expanded base universe (100+ stocks) ────────────────────────────────────
BASE_UNIVERSE = [
    # Nifty 50
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HCLTECH",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "TATAMOTORS",  # keep old for fallback
    "AXISBANK", "SUNPHARMA", "BAJFINANCE", "TITAN", "MARUTI", "WIPRO",
    "ONGC", "NTPC", "POWERGRID", "ULTRACEMCO", "HINDUNILVR", "M&M",
    "TATASTEEL", "JSWSTEEL", "HDFCLIFE", "SBILIFE", "DRREDDY", "CIPLA",
    "DIVISLAB", "EICHERMOT", "INDUSINDBK", "GRASIM", "BRITANNIA", "COALINDIA",
    "HINDALCO", "BAJAJFINSV", "BPCL", "APOLLOHOSP", "ASIANPAINT", "NESTLEIND",
    "TATACONSUM", "TRENT", "HEROMOTOCO", "SHRIRAMFIN", "ADANIENT", "ADANIPORTS",
    "HDFC", "LICHSGFIN", "BANKBARODA", "PNB", "CANBK", "IOC", "GAIL",
    # Mid cap
    "BEL", "HAL", "COFORGE", "LTIM",  # keep old for fallback
    "TECHM", "MPHASIS", "PERSISTENT",
    "ANGELONE", "ICICIGI", "DMART", "NYKAA", "ZOMATO",  # keep old for fallback
    "PAYTM", "ADANIPOWER", "IREDA", "IRFC", "RVNL", "HUDCO", "RAILTEL",
    "CUPID", "BLUESTONE", "JIOFIN", "BSE", "CDSL", "NSDL", "NSE",
] + EXTRA_NEW_SYMBOLS  # add the new symbols so they are scanned

# ── Redis helpers ─────────────────────────────────────────────────────────
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
        _redis_set(SEARCHED_KEY, searched[-200:])

# ── Real-time IPO fetcher ───────────────────────────────────────────────────
def _get_recent_ipos() -> List[str]:
    """Fetch recently listed IPOs from NSE API, with Redis cache."""
    cached = _redis_get(IPO_CACHE_KEY)
    if cached:
        logger.info("Using cached IPO list: %d symbols", len(cached))
        return cached

    symbols = []
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = httpx.get("https://www.nseindia.com/api/ipo?type=listed", headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            for item in data:
                sym = item.get("symbol") or item.get("secCode")
                if sym:
                    symbols.append(sym.upper())
            logger.info("Fetched %d IPOs from NSE API", len(symbols))
        else:
            logger.warning("NSE IPO API returned status %d", resp.status_code)
    except Exception as e:
        logger.warning("Failed to fetch recent IPOs from NSE: %s", e)

    if not symbols:
        fallback = ["JIOFIN", "BLUESTONE", "CUPID", "IREDA", "RVNL", "HUDCO", "RAILTEL", "IRFC", "ZOMATO", "NYKAA", "PAYTM"]
        symbols = fallback
        logger.info("Using fallback IPO list")

    _redis_set(IPO_CACHE_KEY, symbols, ttl=86400)
    return symbols

# ── Momentum movers ────────────────────────────────────────────────────────
def _get_momentum_movers() -> List[str]:
    """Fetch top 10 gainers and top 10 losers from Nifty 50."""
    movers = []
    try:
        nifty50_symbols = [
            "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
            "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BHARTIARTL", "BPCL",
            "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
            "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
            "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC",
            "INDUSINDBK", "INFY", "JSWSTEEL", "KOTAKBANK", "LT",
            "LTIM", "M&M", "MARUTI", "NESTLEIND", "NTPC",
            "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
            "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL",
            "TCS", "TRENT", "TITAN", "ULTRACEMCO", "WIPRO",
        ]
        performances = []
        for sym in nifty50_symbols:
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
        movers = [s for s, _ in performances[:10]] + [s for s, _ in performances[-10:]]
        logger.info("Momentum movers: %s", movers[:5])
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
        text = " ".join(e.title for e in feed.entries[:30]).upper()
        for sym in BASE_UNIVERSE:
            if sym in text:
                mentioned.append(sym)
    except Exception as e:
        logger.warning("Could not parse news for symbols: %s", e)
    return mentioned[:15]

# ── Symbol resolution (auto-correction + alias mapping) ────────────────────
def _get_all_known_symbols() -> Set[str]:
    """Combine all sources of known symbols (cached for 6 hours)."""
    cached = _redis_get(KNOWN_SYMBOLS_KEY)
    if cached:
        return set(cached)

    combined = set(BASE_UNIVERSE)
    combined.update(_load_watchlist())
    combined.update(_load_searched())
    combined.update(_get_recent_ipos())
    combined.update(_get_momentum_movers())
    # Also add any alias targets
    for target in SYMBOL_ALIASES.values():
        if isinstance(target, list):
            combined.update(target)
        else:
            combined.add(target)

    scan_universe = _redis_get(SCAN_UNIVERSE_KEY)
    if scan_universe:
        combined.update(scan_universe)

    cleaned = set()
    for s in combined:
        s = s.upper().replace(".NS", "").replace(".BO", "")
        if s:
            cleaned.add(s)

    _redis_set(KNOWN_SYMBOLS_KEY, list(cleaned), ttl=21600)
    return cleaned

def _resolve_symbol(misspelled: str) -> Optional[str]:
    """
    Try to correct a misspelled symbol using:
    1. Alias mapping (old → new)
    2. Fuzzy matching against known symbols
    """
    if not misspelled:
        return None
    symbol = misspelled.upper().replace(".NS", "").replace(".BO", "")

    # Check alias map
    if symbol in SYMBOL_ALIASES:
        alias = SYMBOL_ALIASES[symbol]
        if isinstance(alias, list):
            # For splits, return the first one as primary, but also log
            logger.info("Alias '%s' → %s (primary)", symbol, alias[0])
            return alias[0]
        else:
            logger.info("Alias '%s' → %s", symbol, alias)
            return alias

    known = _get_all_known_symbols()

    # Exact match
    if symbol in known:
        return symbol

    # Fuzzy match
    matches = difflib.get_close_matches(symbol, known, n=1, cutoff=0.7)
    if matches:
        corrected = matches[0]
        logger.info("Corrected '%s' → '%s'", symbol, corrected)
        return corrected

    return None

# ── Hinglish natural-language summary generator ──────────────────────────
def _generate_summary(data: dict) -> str:
    """Generate a short, conversational Hinglish summary for the decision."""
    decision = data.get("decision")
    symbol = data.get("symbol")
    confidence = data.get("confidence")
    combined_score = data.get("combined_score")
    entry = data.get("entry_range", {})
    target = data.get("target")
    stop = data.get("stop_loss")
    holding = data.get("holding_period")
    reasons = data.get("reasons", {})
    close = data.get("close")

    if decision == "BUY NOW":
        summary = f"🚀 {symbol} अभी खरीदने का बहुत अच्छा मौका है! "
        summary += f"एंट्री {entry.get('low')}-{entry.get('high')}, टारगेट {target}, स्टॉप लॉस {stop}. "
        summary += f"अनुमानित होल्डिंग अवधि {holding}. "
        summary += f"कॉन्फिडेंस {confidence} है, स्कोर {combined_score}. "
        tech = reasons.get("technical", [])
        if tech:
            summary += f"तकनीकी: {tech[0]}. "
        fund = reasons.get("fundamental", [])
        if fund:
            summary += f"फंडामेंटल: {fund[0]}. "
        summary += "जल्दी से अपने पोर्टफोलियो में शामिल करें!"
    elif decision == "PREPARE TO BUY":
        summary = f"⏳ {symbol} के लिए खरीदारी की तैयारी करें, लेकिन अभी थोड़ा इंतज़ार करें. "
        summary += f"एंट्री {entry.get('low')}-{entry.get('high')}, टारगेट {target}, स्टॉप लॉस {stop}. "
        summary += f"होल्डिंग {holding} हो सकती है. स्कोर {combined_score}. "
        summary += "अगले कुछ दिनों में वॉल्यूम और ट्रेंड कन्फर्मेशन का इंतज़ार करें."
    elif decision == "HOLD":
        summary = f"🔄 {symbol} को होल्ड करें. अभी बेचने की ज़रूरत नहीं है. "
        summary += f"टारगेट {target} पर पहुंचने पर विचार करें. स्टॉप लॉस {stop}. "
        summary += f"स्कोर {combined_score}, स्थिति स्थिर है."
    elif decision == "SELL":
        summary = f"🔴 {symbol} को बेचने का समय आ गया है. "
        summary += f"मौजूदा कीमत {close} है, टारगेट से नीचे. "
        summary += f"स्टॉप लॉस {stop} पार कर चुके हैं. "
        summary += f"स्कोर {combined_score}, कमज़ोर दिख रहा है. जल्दी से निकलें!"
    else:  # DO NOT BUY
        summary = f"❌ {symbol} अभी न खरीदें. "
        summary += f"तकनीकी और फंडामेंटल दोनों कमज़ोर हैं, स्कोर {combined_score}. "
        tech = reasons.get("technical", [])
        if tech:
            summary += f"तकनीकी: {tech[0]}. "
        fund = reasons.get("fundamental", [])
        if fund:
            summary += f"फंडामेंटल: {fund[0]}. "
        summary += "बेहतर होगा कि कुछ दिन और देखें."
    return summary

# ── Build scan universe ──────────────────────────────────────────────────────
def _build_scan_universe() -> List[str]:
    """
    Build a fresh scan universe by combining:
    1. Base liquid universe (100+ stocks)
    2. User watchlist
    3. Previously searched symbols
    4. Weekly momentum movers (top 10 gainers + top 10 losers)
    5. News-mentioned symbols
    6. Real-time IPOs (from NSE API)
    Deduped and capped at 120 symbols.
    """
    cached = _redis_get(SCAN_UNIVERSE_KEY)
    if cached:
        return cached

    universe = set(BASE_UNIVERSE)
    universe.update(_load_watchlist())
    universe.update(_load_searched())
    universe.update(_get_momentum_movers())
    universe.update(_get_news_mentioned_symbols())
    universe.update(_get_recent_ipos())

    clean = []
    seen = set()
    for s in universe:
        s = s.upper().replace(".NS", "").replace(".BO", "")
        if s and s not in seen:
            seen.add(s)
            clean.append(s)

    result = clean[:120]
    _redis_set(SCAN_UNIVERSE_KEY, result, ttl=21600)
    logger.info("Scan universe built: %d symbols", len(result))
    return result

# ── Pydantic models ──────────────────────────────────────────────────────────
class WatchlistUpdate(BaseModel):
    symbols: List[str]

class NotificationChannelUpdate(BaseModel):
    discord_webhook_url: str | None = None
    slack_webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    enabled: dict | None = None

# ── Routes ──────────────────────────────────────────────────────────────────
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
            "/stock/{symbol}": "GET – get decision for a symbol (auto-corrects misspelled symbols)",
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
    async def check(name: str, url: str, required: bool):
        if not url:
            return name, {"ok": False, "required": required, "status": "not_configured", "url": None}
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=70) as client:
                resp = await client.get(f"{url.rstrip('/')}/health")
            elapsed = round(time.monotonic() - start, 1)
            if resp.status_code == 200:
                return name, {"ok": True, "required": required, "status": "up", "seconds": elapsed, "url": url}
            return name, {
                "ok": False,
                "required": required,
                "status": f"http_{resp.status_code}",
                "seconds": elapsed,
                "url": url,
            }
        except httpx.HTTPError as e:
            elapsed = round(time.monotonic() - start, 1)
            return name, {
                "ok": False,
                "required": required,
                "status": "unreachable",
                "seconds": elapsed,
                "error": str(e)[:200],
                "url": url,
            }

    results = await asyncio.gather(
        *(check(name, cfg["url"], cfg["required"]) for name, cfg in SYSTEM_SERVICES.items())
    )
    services = {
        "api-gateway": {
            "ok": True,
            "required": True,
            "status": "up",
            "seconds": 0,
            "url": None  # gateway URL is not needed for wake
        }
    }
    services.update(dict(results))
    required_ok = all(v["ok"] for v in services.values() if v["required"])
    all_ok = all(v["ok"] for v in services.values())
    return {"required_ok": required_ok, "all_ok": all_ok, "services": services}

# ── Watchlist endpoints ────────────────────────────────────────────────────
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

# ── Searched symbols ────────────────────────────────────────────────────────
@app.get("/searched")
def get_searched_symbols():
    return {"symbols": _load_searched()}

# ── Stock decision (with auto-correction, alias mapping, and summary) ──────
@app.get("/stock/{symbol}")
def get_stock_decision(symbol: str, already_owned: bool = False):
    original = symbol.strip()
    resolved = _resolve_symbol(original)

    if resolved is None:
        resolved = original.upper()
        corrected_from = None
    elif resolved != original.upper():
        corrected_from = original.upper()
        symbol_to_use = resolved
    else:
        symbol_to_use = original.upper()
        corrected_from = None

    _add_searched(symbol_to_use)

    if _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass

    try:
        resp = httpx.get(
            f"{DECISION_URL}/decide/{symbol_to_use}",
            params={"already_owned": already_owned},
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()

        if corrected_from:
            result["corrected_from"] = corrected_from
            result["symbol"] = symbol_to_use

        # Add natural language summary
        result["natural_language_summary"] = _generate_summary(result)

        return result

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            suggestions = difflib.get_close_matches(symbol_to_use, _get_all_known_symbols(), n=3, cutoff=0.5)
            suggestion_text = f"Symbol '{symbol_to_use}' not found. Did you mean: {', '.join(suggestions)}?" if suggestions else f"Symbol '{symbol_to_use}' not found."
            raise HTTPException(status_code=404, detail=suggestion_text)
        else:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Decision engine unreachable: {e}")

# ── Scan ──────────────────────────────────────────────────────────────────
@app.get("/scan")
def run_scan(force_refresh: bool = False):
    if force_refresh and _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass

    universe = _build_scan_universe()
    results = []
    errors = []

    with httpx.Client(timeout=150) as client:
        for symbol in universe:
            try:
                resp = client.get(f"{DECISION_URL}/decide/{symbol}")
                resp.raise_for_status()
                result = resp.json()
                # Add summary to each result
                result["natural_language_summary"] = _generate_summary(result)
                results.append(result)
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

# ── Universe preview endpoints ──────────────────────────────────────────────
@app.get("/scan/universe")
def get_scan_universe():
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
    if _redis:
        try:
            _redis.delete(SCAN_UNIVERSE_KEY)
        except Exception:
            pass
    return {"message": "Scan universe cache cleared — will rebuild on next scan"}

# ── Notification endpoints ──────────────────────────────────────────────────
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