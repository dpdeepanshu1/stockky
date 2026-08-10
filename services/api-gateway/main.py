"""
API Gateway
------------
Single entry point for the React frontend.
v2.5.3 – adds stale cache fallback for /market/indices to avoid 500s on rate limit.
"""
import os
import json
import time
import asyncio
import logging
import difflib
import uuid
from datetime import datetime
from typing import List, Optional, Set, Dict, Union

import httpx
import yfinance as yf
import feedparser
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from upstash_redis import Redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api-gateway")

# ---- Live Render URLs ----
DECISION_URL = os.getenv("DECISION_URL", "https://decision-engine-service-0hg6.onrender.com")
NOTIFICATION_URL = os.getenv("NOTIFICATION_URL", "https://notification-service-36py.onrender.com")
NEWS_URL = os.getenv("NEWS_URL", "https://news-intelligence-service.onrender.com")
MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://stockky-market-data.onrender.com")
TECHNICAL_URL = os.getenv("TECHNICAL_URL", "https://technical-analysis-service-zhnc.onrender.com")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", "https://fundamental-analysis-service.onrender.com")
EVENT_URL = os.getenv("EVENT_URL", "https://event-tracker-service-m1lw.onrender.com")
PREDICTION_URL = os.getenv("PREDICTION_URL", "https://prediction-service-wowb.onrender.com")

# ---- Market Sentiment & Training ----
MARKET_SENTIMENT_URL = os.getenv("MARKET_SENTIMENT_URL", "https://market-sentiment-service.onrender.com")
TRAINING_URL = os.getenv("TRAINING_URL", "https://training-service-5e9v.onrender.com")

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
    "market-sentiment": {"url": MARKET_SENTIMENT_URL, "required": False},
    "training": {"url": TRAINING_URL, "required": False},
}

app = FastAPI(title="Stockky API Gateway", version="2.5.3")

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_cors_header(request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response

@app.exception_handler(Exception)
async def universal_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {str(exc)}"},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*"
        }
    )

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
SCAN_TASK_PREFIX    = "stockky:scan_task:"
MARKET_MOVERS_CACHE_PREFIX = "stockky:market_movers:"
INDICES_CACHE_KEY   = "stockky:indices"          # NEW for indices caching

# Cache keys for fundamental and events
FUNDAMENTAL_CACHE_PREFIX = "stockky:fundamental:"
EVENT_CACHE_PREFIX = "stockky:event:"

# ── Symbol Aliases ──────────────────────────────────────────────────────────
SYMBOL_ALIASES: Dict[str, Union[str, List[str]]] = {
    "TATAMOTORS": "TMPV",
    "TATAMOTER": "TMPV",
    "TATAMOT": "TMPV",
    "LTIM": "LTM",
    "LTIMIND": "LTM",
    "LTIMINDTREE": "LTM",
    "ZOMATO": "ETERNAL",
    "ZOMAT": "ETERNAL",
}
EXTRA_NEW_SYMBOLS = ["TMPV", "TMLCV", "LTM", "ETERNAL"]

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
    return _redis_get(WATCHLIST_KEY) or []

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

# ── Dynamic Universe Sources (unchanged) ──────────────────────────────────
_nse_client = None

def _get_nse_client() -> httpx.Client:
    global _nse_client
    if _nse_client is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
            "DNT": "1",
        }
        _nse_client = httpx.Client(headers=headers, timeout=15)
        _nse_client.get("https://www.nseindia.com")
    return _nse_client

def _fetch_from_nse_api(endpoint: str, cache_key: str, ttl: int = 21600):
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached
    try:
        client = _get_nse_client()
        url = f"https://www.nseindia.com/api/{endpoint}"
        resp = client.get(url)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                _redis_set(cache_key, data, ttl)
                return data
        else:
            logger.warning(f"NSE API {endpoint} returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Failed to fetch {endpoint}: {e}")
    if cached:
        return cached
    return None

def _get_all_nse_securities() -> List[str]:
    data = _fetch_from_nse_api("equity-stockIndices?index=SECURITIES%20IN%20NSE", "nse:all_securities")
    symbols = []
    if data and "data" in data and isinstance(data["data"], list):
        for item in data["data"]:
            if isinstance(item, dict) and item.get("symbol"):
                symbols.append(item["symbol"].upper())
    logger.info(f"Fetched {len(symbols)} securities from NSE")
    if not symbols:
        symbols = [
            "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HCLTECH",
            "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "M&M", "MARUTI",
            "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "SBILIFE", "SUNPHARMA",
            "TATAMOTORS", "TATASTEEL", "WIPRO", "ADANIENT", "ADANIPORTS",
            "ASIANPAINT", "AXISBANK", "BAJAJFINSV", "BRITANNIA", "CIPLA",
            "COALINDIA", "DIVISLAB", "DRREDDY", "EICHERMOT", "GRASIM",
            "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "INDUSINDBK",
            "JSWSTEEL", "LTIM", "SHRIRAMFIN", "TATACONSUM", "TRENT", "TITAN",
            "ULTRACEMCO", "BAJAJ-AUTO", "BPCL", "APOLLOHOSP", "BAJFINANCE",
            "BANDHANBNK", "BIOCON", "BOSCHLTD", "CHOLAFIN", "DABUR", "DALBHARAT",
            "DIXON", "DMART", "ESCORTS", "FEDERALBNK", "GODREJCP", "GODREJPROP",
            "HAVELLS", "HINDZINC", "IOC", "IRCTC", "LICHSGFIN", "MUTHOOTFIN",
            "NAUKRI", "NMDC", "PAGEIND", "PETRONET", "PIIND", "PNB", "RBLBANK",
            "SAIL", "SRTRANSFIN", "TATACOMM", "TECHM", "TORNTPHARM", "VEDL",
            "ZOMATO", "IDEA", "ABFRL", "BANKBARODA", "BHEL", "CANBK", "HAL",
            "IBULHSGFIN", "JINDALSTEL", "JUBLFOOD", "MCDOWELL-N", "MPHASIS",
            "PIDILITIND", "SIEMENS", "UPL", "VBL", "YESBANK", "GAIL",
            "AARTIIND", "ABB", "ADANIGREEN", "ADANITRANS", "ALKEM", "AMBER",
            "ASHOKLEY", "ASTRAZEN", "AUROPHARMA", "BALKRISIND", "BERGEPAINT",
            "BLUESTARCO", "CARBORUNIV", "CENTRALBK", "CGPOWER", "CISCO", "COCHINSHIP",
            "COROMANDEL", "CROMPTON", "CUMMINSIND", "DELTACORP", "DIVISLAB",
            "DLF", "EIDPARRY", "EXIDEIND", "FORTIS", "GMRINFRA", "GODREJIND",
            "GREENPLY", "HINDPETRO", "IDEA", "INDIAMART", "INDIGO", "JSWENERGY",
            "JUBILANT", "KPITTECH", "KPRMILL", "LALPATHLAB", "LUPIN", "MCX",
            "MINDACORP", "MOTHERSUMI", "NATCOPHARM", "NAVINFLUOR", "NEULANDLAB",
            "NILKAMAL", "NLCINDIA", "OIL", "PERSISTENT", "PFC", "PHOENIXLTD",
            "PRESTIGE", "RAYMOND", "RECLTD", "RENUKA", "RITES", "RVNL",
            "SCHAEFFLER", "SHREECEM", "SONATSOFTW", "SUNTV", "SUPRAJIT",
            "SYRMA", "TATAELXSI", "TATAMTRDVR", "TATAPOWER", "TATATECH",
            "TIMKEN", "TORNTPHARM", "TRIDENT", "TVSMOTOR", "WELSPUNIND", "WHIRLPOOL",
            "WOCKPHARMA", "ZEEL", "ZYDUSWELL"
        ]
        logger.warning(f"Using enhanced static fallback list with {len(symbols)} symbols")
    return symbols

def _get_nifty_indices() -> List[str]:
    indices = ["NIFTY%2050", "NIFTY%20NEXT%2050", "NIFTY%20MIDCAP%20100"]
    all_symbols = []
    for idx in indices:
        data = _fetch_from_nse_api(f"equity-stockIndices?index={idx}", f"nse:index_{idx}")
        if data and "data" in data and isinstance(data["data"], list):
            for item in data["data"]:
                if isinstance(item, dict) and item.get("symbol"):
                    all_symbols.append(item["symbol"].upper())
    fallback = [
        "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
        "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BHARTIARTL", "BPCL",
        "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
        "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
        "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC",
        "INDUSINDBK", "INFY", "JSWSTEEL", "KOTAKBANK", "LT",
        "LTIM", "M&M", "MARUTI", "NESTLEIND", "NTPC",
        "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
        "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL",
        "TCS", "TRENT", "TITAN", "ULTRACEMCO", "WIPRO"
    ]
    all_symbols = list(set(all_symbols + fallback))
    return all_symbols

def _get_recent_ipos() -> List[str]:
    data = _fetch_from_nse_api("ipo?type=listed", IPO_CACHE_KEY, ttl=86400)
    symbols = []
    if data and isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                sym = item.get("symbol") or item.get("secCode")
                if sym:
                    symbols.append(sym.upper())
    if not symbols:
        symbols = ["JIOFIN", "BLUESTONE", "CUPID", "IREDA", "RVNL", "HUDCO", "RAILTEL", "IRFC", "MVELECTRO"]
    return symbols

def _get_momentum_movers() -> List[str]:
    movers = []
    try:
        nifty_symbols = _get_nifty_indices()[:50]
        performances = []
        for sym in nifty_symbols:
            try:
                ticker = yf.Ticker(f"{sym}.NS")
                hist = ticker.history(period="5d", interval="1d")
                if hist.empty or len(hist) < 2:
                    continue
                week_change = (hist["Close"].iloc[-1] - hist["Close"].iloc[0]) / hist["Close"].iloc[0] * 100
                performances.append((sym, float(week_change)))
            except Exception:
                continue
        performances.sort(key=lambda x: x[1], reverse=True)
        movers = [s for s, _ in performances[:10]] + [s for s, _ in performances[-10:]]
    except Exception as e:
        logger.warning("Could not fetch momentum movers: %s", e)
    return movers

def _get_news_mentioned_symbols() -> List[str]:
    mentioned = []
    try:
        feed = feedparser.parse(
            "https://news.google.com/rss/search?q=NSE+stock+bulk+deal+earnings+results&hl=en-IN&gl=IN&ceid=IN:en"
        )
        text = " ".join(e.title for e in feed.entries[:30]).upper()
        all_symbols = _get_all_nse_securities()
        for sym in all_symbols[:300]:
            if sym in text:
                mentioned.append(sym)
    except Exception as e:
        logger.warning("Could not parse news for symbols: %s", e)
    return mentioned[:15]

def _get_event_symbols() -> List[str]:
    try:
        resp = httpx.get(f"{EVENT_URL}/symbols_with_events", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data.get("symbols", [])
            elif isinstance(data, list):
                return data
    except Exception as e:
        logger.warning(f"Could not fetch event symbols: {e}")
    return []

# ── Build scan universe ──────────────────────────────────────────────────────
def _build_scan_universe() -> List[str]:
    cached = _redis_get(SCAN_UNIVERSE_KEY)
    if cached and isinstance(cached, list) and len(cached) > 0:
        return cached

    universe = set()
    try:
        all_stocks = _get_all_nse_securities()
        if all_stocks:
            universe.update(all_stocks[:300])
        else:
            universe.update(_get_nifty_indices())
    except Exception as e:
        logger.warning(f"Failed to fetch securities: {e}")
        universe.update(_get_nifty_indices())

    try:
        universe.update(_get_nifty_indices())
    except Exception as e:
        logger.warning(f"Failed to fetch indices: {e}")

    try:
        universe.update(_get_momentum_movers())
    except Exception as e:
        logger.warning(f"Failed to fetch momentum movers: {e}")

    try:
        universe.update(_get_news_mentioned_symbols())
    except Exception as e:
        logger.warning(f"Failed to fetch news symbols: {e}")

    try:
        universe.update(_get_recent_ipos())
    except Exception as e:
        logger.warning(f"Failed to fetch IPOs: {e}")

    try:
        universe.update(_get_event_symbols())
    except Exception as e:
        logger.warning(f"Failed to fetch event symbols: {e}")

    universe.update(_load_watchlist())
    universe.update(_load_searched())
    for target in SYMBOL_ALIASES.values():
        if isinstance(target, list):
            universe.update(target)
        else:
            universe.add(target)

    clean = []
    seen = set()
    for s in universe:
        s = s.upper().replace(".NS", "").replace(".BO", "")
        if s and s not in seen:
            seen.add(s)
            clean.append(s)

    if not clean:
        fallback = ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HCLTECH", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK"]
        clean = fallback

    result = clean[:300]
    _redis_set(SCAN_UNIVERSE_KEY, result, ttl=21600)
    logger.info(f"Scan universe built: {len(result)} symbols")
    return result

# ── Symbol resolution ──────────────────────────────────────────────────────
def _get_all_known_symbols() -> Set[str]:
    cached = _redis_get(KNOWN_SYMBOLS_KEY)
    if cached and isinstance(cached, list):
        return set(cached)
    combined = set()
    try:
        combined.update(_get_all_nse_securities()[:300])
    except:
        pass
    combined.update(_get_nifty_indices())
    combined.update(_load_watchlist())
    combined.update(_load_searched())
    combined.update(_get_recent_ipos())
    combined.update(_get_momentum_movers())
    for target in SYMBOL_ALIASES.values():
        if isinstance(target, list):
            combined.update(target)
        else:
            combined.add(target)
    scan_universe = _redis_get(SCAN_UNIVERSE_KEY)
    if scan_universe and isinstance(scan_universe, list):
        combined.update(scan_universe)
    cleaned = set()
    for s in combined:
        s = s.upper().replace(".NS", "").replace(".BO", "")
        if s:
            cleaned.add(s)
    _redis_set(KNOWN_SYMBOLS_KEY, list(cleaned), ttl=21600)
    return cleaned

def _resolve_symbol(misspelled: str) -> Optional[str]:
    if not misspelled:
        return None
    symbol = misspelled.upper().replace(".NS", "").replace(".BO", "")
    if symbol in SYMBOL_ALIASES:
        alias = SYMBOL_ALIASES[symbol]
        if isinstance(alias, list):
            return alias[0]
        return alias
    known = _get_all_known_symbols()
    if symbol in known:
        return symbol
    matches = difflib.get_close_matches(symbol, known, n=1, cutoff=0.7)
    if matches:
        return matches[0]
    return None

# ── Safe response normalization ──────────────────────────────────────────
def _normalize_decision_response(raw, symbol: str) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    default = {
        "symbol": symbol,
        "decision": "DO NOT BUY",
        "confidence": "Low",
        "combined_score": 0,
        "technical_score": 50,
        "fundamental_score": 50,
        "news_score": None,
        "prediction_score": None,
        "prediction_note": None,
        "market_score": 50,
        "training_score": 50,
        "event_risk": False,
        "entry_range": None,
        "target": None,
        "stop_loss": None,
        "holding_period": "N/A",
        "close": None,
        "support": None,
        "resistance": None,
        "reasons": {
            "technical": ["Data unavailable"],
            "fundamental": ["Data unavailable"]
        },
        "valuation": "fair",
        "sector": None,
        "data_insufficient": False,
        "fundamental_metrics": None,
        "fundamental_fallback": False,
    }
    merged = {**default, **raw}
    return merged

# ── Fallback helpers with caching ──────────────────────────────────────────
def _fetch_price_from_quote(symbol: str) -> Optional[float]:
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            price = data.get("price")
            if price is not None:
                logger.info(f"Price fallback for {symbol}: ₹{price}")
                return price
        else:
            logger.warning(f"Quote endpoint returned {resp.status_code} for {symbol}")
    except Exception as e:
        logger.warning(f"Price fetch failed for {symbol}: {e}")
    return None

async def _fetch_fundamental_cached(symbol: str, client: httpx.AsyncClient) -> tuple[Optional[dict], bool]:
    cache_key = f"{FUNDAMENTAL_CACHE_PREFIX}{symbol}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached.get("metrics"), cached.get("fallback", False)

    try:
        resp = await client.get(f"{FUNDAMENTAL_URL}/analyze/{symbol}", timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            metrics = data.get("metrics")
            fallback_used = data.get("fallback_used", False)
            _redis_set(cache_key, {"metrics": metrics, "fallback": fallback_used}, ttl=21600)
            return metrics, fallback_used
    except Exception as e:
        logger.warning(f"Fundamental fetch failed for {symbol}: {e}")
    return {}, True

async def _fetch_events_cached(symbol: str, client: httpx.AsyncClient) -> Optional[dict]:
    cache_key = f"{EVENT_CACHE_PREFIX}{symbol}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached

    try:
        resp = await client.get(f"{EVENT_URL}/events/{symbol}", timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, dict):
                _redis_set(cache_key, data, ttl=21600)
                return data
    except Exception as e:
        logger.warning(f"Events fetch failed for {symbol}: {e}")
    return None

async def _fetch_news_cached(symbol: str, client: httpx.AsyncClient) -> Optional[dict]:
    try:
        resp = await client.get(f"{NEWS_URL}/analyze/{symbol}", timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning(f"News fetch failed for {symbol}: {e}")
    return None

async def _fetch_prediction_cached(symbol: str, client: httpx.AsyncClient) -> tuple[Optional[float], Optional[str]]:
    try:
        resp = await client.get(f"{PREDICTION_URL}/predict/{symbol}", timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("model_loaded"):
                return data.get("prediction_score"), data.get("note")
    except Exception as e:
        logger.warning(f"Prediction lookup failed for {symbol}: {e}")
    return None, None

# ── Hinglish & GenAI summary ──────────────────────────────────────────────
def _generate_summary(data) -> str:
    if not data or not isinstance(data, dict):
        return "Data unavailable"
    decision = data.get("decision")
    symbol = data.get("symbol") or "Unknown"
    confidence = data.get("confidence")
    combined_score = data.get("combined_score")
    entry = data.get("entry_range") or {}
    target = data.get("target")
    stop = data.get("stop_loss")
    holding = data.get("holding_period")
    reasons = data.get("reasons") or {}
    close = data.get("close")
    prediction_note = data.get("prediction_note")

    if decision == "BUY NOW":
        summary = f"🚀 {symbol} अभी खरीदने का बहुत अच्छा मौका है! "
        summary += f"एंट्री {entry.get('low')}-{entry.get('high')}, टारगेट {target}, स्टॉप लॉस {stop}. "
        summary += f"होल्डिंग {holding}. कॉन्फिडेंस {confidence}, स्कोर {combined_score}. "
        tech = reasons.get("technical", [])
        if tech:
            summary += f"तकनीकी: {tech[0]}. "
        fund = reasons.get("fundamental", [])
        if fund:
            summary += f"फंडामेंटल: {fund[0]}. "
        summary += "जल्दी शामिल करें!"
    elif decision == "PREPARE TO BUY":
        summary = f"⏳ {symbol} के लिए, तैयारी करें, अभी इंतज़ार करें. "
        summary += f"एंट्री {entry.get('low')}-{entry.get('high')}, टारगेट {target}, स्टॉप {stop}. "
        summary += f"स्कोर {combined_score}. वॉल्यूम कन्फर्मेशन का इंतज़ार करें."
    elif decision == "HOLD":
        summary = f"🔄 {symbol} को होल्ड करें. टारगेट {target}, स्टॉप {stop}. स्कोर {combined_score}."
    elif decision == "SELL":
        summary = f"🔴 {symbol} को बेचें. कीमत {close}, टारगेट से नीचे. स्टॉप {stop} पार. स्कोर {combined_score}."
    else:
        summary = f"❌ {symbol} अभी न खरीदें. स्कोर {combined_score}. "
        tech = reasons.get("technical", [])
        if tech:
            summary += f"तकनीकी: {tech[0]}. "
        fund = reasons.get("fundamental", [])
        if fund:
            summary += f"फंडामेंटल: {fund[0]}. "
        summary += "कुछ दिन और देखें."

    if prediction_note:
        summary += f" 🤖 {prediction_note}"

    return summary

# ── Telegram notification helper ──────────────────────────────────────────
def _send_scan_notification(recommendations: list, verdict: str, scanned: int, universe_size: int):
    if not recommendations:
        message = f"📊 Market Scan Complete\n\nScanned {scanned} stocks. No strong BUY signals today.\nVerdict: {verdict}"
    else:
        lines = [f"📊 *Top {len(recommendations)} Picks from Market Scan*", ""]
        for i, r in enumerate(recommendations[:5], 1):
            symbol = r.get("symbol")
            decision = r.get("decision")
            combined_score = r.get("combined_score")
            close = r.get("close")
            target = r.get("target")
            stop_loss = r.get("stop_loss")
            entry = r.get("entry_range") or {}
            entry_low = entry.get("low")
            entry_high = entry.get("high")
            lines.append(f"{i}. *{symbol}* – {decision} (Score: {combined_score})")
            if close:
                lines.append(f"   Current: ₹{close:.2f}")
            if entry_low and entry_high:
                lines.append(f"   Entry: ₹{entry_low:.2f} – ₹{entry_high:.2f}")
            if target:
                upside = ((target - close) / close * 100) if close else 0
                lines.append(f"   Target: ₹{target:.2f} (+{upside:.1f}%)")
            if stop_loss:
                lines.append(f"   Stop: ₹{stop_loss:.2f}")
            lines.append("")
        message = "\n".join(lines)

    try:
        _wake_notification_service()
        resp = httpx.post(f"{NOTIFICATION_URL}/notify", json={
            "title": "Market Scan Complete",
            "message": message,
            "channel": "telegram"
        }, timeout=10)
        if resp.status_code == 200:
            logger.info("Scan recommendations sent to Telegram")
        else:
            logger.warning("Telegram notification failed with status %d", resp.status_code)
    except Exception as e:
        logger.warning("Failed to send scan notification: %s", e)

def _wake_notification_service() -> bool:
    try:
        resp = httpx.get(f"{NOTIFICATION_URL}/health", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False

# ============================================================================
# ⚡ ULTRA-FAST PARALLEL SCAN with internal parallelism and caching
# ============================================================================

MAX_PARALLEL_WORKERS = int(os.getenv("MAX_PARALLEL_SCAN_WORKERS", "20"))
MAX_RETRIES = 2
RETRY_BACKOFF = 1.5

async def _analyze_one_symbol_ultra(
    symbol: str,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore
) -> dict:
    """
    Analyse one symbol with parallel internal calls and caching.
    All timeouts increased to 60s for reliability.
    """
    async with sem:
        for attempt in range(MAX_RETRIES + 1):
            try:
                # === 1. Fetch decision engine (primary) ===
                decision_resp = await client.get(f"{DECISION_URL}/decide/{symbol}", timeout=60)
                decision_resp.raise_for_status()
                raw = decision_resp.json()
                normalized = _normalize_decision_response(raw, symbol)

                # === 2. Fetch price if missing ===
                if normalized.get("close") is None:
                    price = _fetch_price_from_quote(symbol)
                    if price is not None:
                        normalized["close"] = price
                        if normalized.get("support") is None:
                            normalized["support"] = round(price * 0.95, 2)
                        if normalized.get("resistance") is None:
                            normalized["resistance"] = round(price * 1.05, 2)

                # === 3. Parallel fetch fundamentals, events, news, prediction ===
                fund_task = _fetch_fundamental_cached(symbol, client)
                event_task = _fetch_events_cached(symbol, client)
                news_task = _fetch_news_cached(symbol, client)
                pred_task = _fetch_prediction_cached(symbol, client)

                fund_metrics, fund_fallback = await fund_task
                event_data = await event_task
                news_data = await news_task
                pred_score, pred_note = await pred_task

                # === 4. Merge results ===
                if fund_metrics:
                    normalized["fundamental_metrics"] = fund_metrics
                    normalized["fundamental_fallback"] = fund_fallback

                if event_data and event_data.get("next_earnings_date"):
                    normalized["event_risk"] = True
                    reasons = normalized.get("reasons", {})
                    reasons["event"] = [f"Earnings due: {event_data['next_earnings_date']}"]
                    normalized["reasons"] = reasons

                if news_data:
                    normalized["news_score"] = news_data.get("news_score")
                    reasons = normalized.get("reasons", {})
                    if news_data.get("reasons"):
                        reasons["news"] = news_data["reasons"]
                        normalized["reasons"] = reasons

                if pred_score is not None:
                    normalized["prediction_score"] = pred_score
                    normalized["prediction_note"] = pred_note

                # === 5. Generate summary ===
                normalized["natural_language_summary"] = _generate_summary(normalized)
                return normalized

            except httpx.HTTPError as e:
                error_type = type(e).__name__
                error_msg = str(e) or f"{error_type} (empty message)"
                logger.warning(
                    f"Scan error for {symbol} (attempt {attempt+1}/{MAX_RETRIES+1}): {error_type} - {error_msg}"
                )
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF ** attempt
                    logger.info(f"Retrying {symbol} in {wait:.1f}s...")
                    await asyncio.sleep(wait)
                    continue
                else:
                    return {
                        "symbol": symbol,
                        "decision": "ERROR",
                        "error": f"{error_type}: {error_msg if error_msg else 'Unknown HTTP error'}"
                    }
            except Exception as e:
                logger.error(f"Unexpected error for {symbol}: {type(e).__name__} - {str(e)}")
                return {
                    "symbol": symbol,
                    "decision": "ERROR",
                    "error": f"Unexpected: {type(e).__name__} - {str(e)}"
                }
        return {"symbol": symbol, "decision": "ERROR", "error": "Max retries exceeded"}

async def run_scan_parallel(task_id: str, universe: List[str]):
    """
    Parallel scan with internal parallelisation per symbol.
    Overall timeout increased to 180s.
    """
    start_time = time.time()
    total = len(universe)
    processed = 0
    results = []
    errors = []

    _redis_set(SCAN_TASK_PREFIX + task_id, {
        "status": "running",
        "total": total,
        "processed": 0,
        "elapsed": 0,
        "result": None,
        "error": None,
    }, ttl=3600)

    sem = asyncio.Semaphore(MAX_PARALLEL_WORKERS)
    limits = httpx.Limits(max_keepalive_connections=200, max_connections=200)
    async with httpx.AsyncClient(timeout=180, limits=limits) as client:
        tasks = [
            _analyze_one_symbol_ultra(sym, client, sem)
            for sym in universe
        ]

        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
                if result.get("decision") == "ERROR":
                    errors.append({"symbol": result.get("symbol"), "error": result.get("error", "Unknown error")})
                else:
                    results.append(result)
            except Exception as e:
                logger.error(f"Task failed: {e}")
            processed += 1
            elapsed = round(time.time() - start_time, 1)
            if processed % 5 == 0 or processed == total:
                _redis_set(SCAN_TASK_PREFIX + task_id, {
                    "status": "running",
                    "total": total,
                    "processed": processed,
                    "elapsed": elapsed,
                    "result": None,
                    "error": None,
                }, ttl=3600)

    # Sort and build result
    results.sort(key=lambda r: r.get("combined_score", 0), reverse=True)

    actionable = [r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")]
    top_picks = actionable[:5]
    watchlist_candidates = []
    if not top_picks:
        watchlist_candidates = results[:3]

    buy_count = len([r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")])
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

    verdict = f"{len(top_picks)} strong opportunity(ies) found" if top_picks else "DO NOT BUY ANY STOCK TODAY — market conditions cautious"

    final_result = {
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

    _redis_set(SCAN_TASK_PREFIX + task_id, {
        "status": "done",
        "total": total,
        "processed": total,
        "elapsed": round(time.time() - start_time, 1),
        "result": final_result,
        "error": None,
    }, ttl=3600)

    _send_scan_notification(final_result.get("recommendations", []), final_result["verdict"], final_result["scanned"], final_result["universe_size"])

# ── Cached Market Movers Data ──────────────────────────────────────────────
def _get_nifty50_data() -> List[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"{MARKET_MOVERS_CACHE_PREFIX}{today}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, list) and len(cached) > 0:
        logger.info("Serving cached market movers data for %s", today)
        return cached

    logger.info("Fetching fresh market movers data from yfinance for %s", today)
    nifty_symbols = _get_nifty_indices()[:50]
    data = []
    for sym in nifty_symbols:
        try:
            ticker = yf.Ticker(f"{sym}.NS")
            hist = ticker.history(period="1d", interval="1m")
            if hist.empty:
                continue
            latest = hist.iloc[-1]
            prev_close = hist.iloc[0]["Close"]
            change_pct = (latest["Close"] - prev_close) / prev_close * 100
            data.append({
                "symbol": sym,
                "price": round(latest["Close"], 2),
                "change": round(latest["Close"] - prev_close, 2),
                "change_pct": round(change_pct, 2),
                "volume": int(latest["Volume"]),
                "high": round(latest["High"], 2),
                "low": round(latest["Low"], 2),
            })
        except Exception as e:
            logger.warning(f"Could not fetch {sym}: {e}")
    _redis_set(cache_key, data, ttl=86400)
    return data

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
        "version": "2.5.3",
        "status": "running",
        "parallel_workers": MAX_PARALLEL_WORKERS,
        "endpoints": {
            "/health": "GET – health check",
            "/ready": "GET – lightweight readiness check",
            "/system/health": "GET – health of all downstream services",
            "/wake/all": "POST – wake all services",
            "/watchlist": "GET/POST – manage watchlist",
            "/watchlist/add": "POST – add symbols",
            "/watchlist/{symbol}": "DELETE – remove symbol",
            "/stock/{symbol}": "GET – get decision for a symbol",
            "/scan": "GET – synchronous scan (legacy)",
            "/scan/start": "POST – start async parallel scan, returns task_id",
            "/scan/status/{task_id}": "GET – get progress/result of async scan",
            "/scan/watchlist": "GET – scan only your watchlist",
            "/scan/universe": "GET – preview current scan universe",
            "/scan/universe/cache": "DELETE – clear universe cache",
            "/searched": "GET – list searched symbols",
            "/market/top-gainers": "GET – top 10 gainers",
            "/market/top-losers": "GET – top 10 losers",
            "/market/most-active": "GET – top 10 most active by volume",
            "/market/trending": "GET – trending stocks (momentum + news)",
            "/market/indices": "GET – live NIFTY 50 & SENSEX points (cached)",
            "/notifications/health": "GET – notification service health",
            "/notifications/config": "GET/POST – get/update notification config",
            "/notifications/config/{channel}": "DELETE – clear a channel",
            "/notifications/test": "POST – test notifications",
            "/notifications/send-picks": "POST – manually send picks to Telegram",
            "/training/status": "GET – get training model status",
            "/training/train": "POST – trigger a new training run",
            "/training/score/{symbol}": "GET – get training intelligence score for a symbol",
            "/docs": "Swagger UI documentation",
        },
    }

@app.get("/health")
def health():
    # Always return instantly – no blocking operations
    return {
        "status": "ok",
        "service": "api-gateway",
        "redis": bool(_redis),
        "ready": True   # frontend expects this
    }

@app.get("/ready")
def ready():
    return {"ready": bool(_redis)}

@app.get("/system/health")
async def system_health():
    # Reduce timeout to 10 seconds per service to avoid hanging
    async def check(name: str, url: str, required: bool):
        if not url:
            return name, {"ok": False, "required": required, "status": "not_configured", "url": None}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{url.rstrip('/')}/health")
            if resp.status_code == 200:
                return name, {"ok": True, "required": required, "status": "up", "url": url}
            return name, {"ok": False, "required": required, "status": f"http_{resp.status_code}", "url": url}
        except Exception as e:
            return name, {"ok": False, "required": required, "status": "unreachable", "error": str(e)[:100], "url": url}

    results = await asyncio.gather(
        *(check(name, cfg["url"], cfg["required"]) for name, cfg in SYSTEM_SERVICES.items())
    )
    services = {"api-gateway": {"ok": True, "required": True, "status": "up", "url": None}}
    services.update(dict(results))
    required_ok = all(v["ok"] for v in services.values() if v["required"])
    all_ok = all(v["ok"] for v in services.values())
    return {"required_ok": required_ok, "all_ok": all_ok, "services": services}

# ── Wake all services ──────────────────────────────────────────────────
@app.post("/wake/all")
async def wake_all_services():
    results = {}
    async with httpx.AsyncClient(timeout=5) as client:
        for name, svc in SYSTEM_SERVICES.items():
            url = svc["url"]
            if not url:
                results[name] = {"ok": False, "error": "no url"}
                continue
            try:
                resp = await client.get(f"{url}/health")
                results[name] = {"ok": resp.status_code == 200, "status": resp.status_code}
            except Exception as e:
                results[name] = {"ok": False, "error": str(e)}
    return {"results": results}

# ── Watchlist endpoints ──────────────────────────────────────────────────────
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

# ── Stock decision ──────────────────────────────────────────────────────────
@app.get("/stock/{symbol}")
def get_stock_decision(symbol: str, already_owned: bool = False):
    original = symbol.strip()
    resolved = _resolve_symbol(original)
    if resolved is None:
        symbol_to_use = original.upper()
        corrected_from = None
    elif resolved != original.upper():
        symbol_to_use = resolved
        corrected_from = original.upper()
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
        raw = resp.json()
        result = _normalize_decision_response(raw, symbol_to_use)

        if result.get("close") is None:
            price = _fetch_price_from_quote(symbol_to_use)
            if price is not None:
                result["close"] = price
                if result.get("support") is None:
                    result["support"] = round(price * 0.95, 2)
                if result.get("resistance") is None:
                    result["resistance"] = round(price * 1.05, 2)

        _merge_fundamentals(result, symbol_to_use)

        if result.get("news_score") is None:
            news = _fetch_news(symbol_to_use)
            if news:
                result["news_score"] = news.get("news_score")
                reasons = result.get("reasons", {})
                if news.get("reasons"):
                    reasons["news"] = news["reasons"]
                    result["reasons"] = reasons

        if result.get("event_risk") is False and not result.get("reasons", {}).get("event"):
            events = _fetch_events(symbol_to_use)
            if events and events.get("next_earnings_date"):
                result["event_risk"] = True
                reasons = result.get("reasons", {})
                reasons["event"] = [f"Earnings due: {events['next_earnings_date']}"]
                result["reasons"] = reasons

        if result.get("prediction_score") is None:
            try:
                pred_resp = httpx.get(f"{PREDICTION_URL}/predict/{symbol_to_use}", timeout=60)
                if pred_resp.status_code == 200:
                    pred_data = pred_resp.json()
                    if pred_data.get("model_loaded"):
                        result["prediction_score"] = pred_data.get("prediction_score")
                        result["prediction_note"] = pred_data.get("note")
            except Exception as e:
                logger.warning(f"Prediction service lookup failed for {symbol_to_use}: {e}")

        if corrected_from:
            result["corrected_from"] = corrected_from
            result["symbol"] = symbol_to_use

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

# ── Legacy sync fallback helpers (kept for completeness) ──────────────────
def _merge_fundamentals(normalized: dict, symbol: str):
    # This is the sync version used by /stock and legacy scan.
    # We'll reuse the cached data if available.
    cache_key = f"{FUNDAMENTAL_CACHE_PREFIX}{symbol}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        metrics = cached.get("metrics")
        fallback_used = cached.get("fallback", False)
        if metrics:
            normalized["fundamental_metrics"] = metrics
            normalized["fundamental_fallback"] = fallback_used
            return

    # Otherwise fetch synchronously
    try:
        resp = httpx.get(f"{FUNDAMENTAL_URL}/analyze/{symbol}", timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            metrics = data.get("metrics")
            fallback_used = data.get("fallback_used", False)
            _redis_set(cache_key, {"metrics": metrics, "fallback": fallback_used}, ttl=21600)
            normalized["fundamental_metrics"] = metrics if metrics else {}
            normalized["fundamental_fallback"] = fallback_used
    except Exception as e:
        logger.warning(f"Fundamental fetch failed for {symbol}: {e}")

def _fetch_news(symbol: str) -> Optional[dict]:
    try:
        resp = httpx.get(f"{NEWS_URL}/analyze/{symbol}", timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning(f"News fetch failed for {symbol}: {e}")
    return None

def _fetch_events(symbol: str) -> Optional[dict]:
    cache_key = f"{EVENT_CACHE_PREFIX}{symbol}"
    cached = _redis_get(cache_key)
    if cached and isinstance(cached, dict):
        return cached
    try:
        resp = httpx.get(f"{EVENT_URL}/events/{symbol}", timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, dict):
                _redis_set(cache_key, data, ttl=21600)
                return data
    except Exception as e:
        logger.warning(f"Events fetch failed for {symbol}: {e}")
    return None

# ── Legacy synchronous scan ──────────────────────────────────────────────────
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
                raw = resp.json()
                normalized = _normalize_decision_response(raw, symbol)

                if normalized.get("close") is None:
                    price = _fetch_price_from_quote(symbol)
                    if price is not None:
                        normalized["close"] = price
                        if normalized.get("support") is None:
                            normalized["support"] = round(price * 0.95, 2)
                        if normalized.get("resistance") is None:
                            normalized["resistance"] = round(price * 1.05, 2)

                _merge_fundamentals(normalized, symbol)

                if normalized.get("news_score") is None:
                    news = _fetch_news(symbol)
                    if news:
                        normalized["news_score"] = news.get("news_score")
                        reasons = normalized.get("reasons", {})
                        if news.get("reasons"):
                            reasons["news"] = news["reasons"]
                            normalized["reasons"] = reasons

                if normalized.get("event_risk") is False and not normalized.get("reasons", {}).get("event"):
                    events = _fetch_events(symbol)
                    if events and events.get("next_earnings_date"):
                        normalized["event_risk"] = True
                        reasons = normalized.get("reasons", {})
                        reasons["event"] = [f"Earnings due: {events['next_earnings_date']}"]
                        normalized["reasons"] = reasons

                if normalized.get("prediction_score") is None:
                    try:
                        pred_resp = client.get(f"{PREDICTION_URL}/predict/{symbol}", timeout=60)
                        if pred_resp.status_code == 200:
                            pred_data = pred_resp.json()
                            if pred_data.get("model_loaded"):
                                normalized["prediction_score"] = pred_data.get("prediction_score")
                                normalized["prediction_note"] = pred_data.get("note")
                    except Exception as e:
                        logger.warning(f"Prediction service lookup failed during scan for {symbol}: {e}")

                normalized["natural_language_summary"] = _generate_summary(normalized)
                results.append(normalized)
            except httpx.HTTPError as e:
                logger.warning("Scan skipped %s: %s", symbol, e)
                errors.append({"symbol": symbol, "error": str(e)})

    results.sort(key=lambda r: r.get("combined_score", 0), reverse=True)
    actionable = [r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")]
    top_picks = actionable[:5]
    watchlist_candidates = []
    if not top_picks:
        watchlist_candidates = results[:3]

    buy_count = len([r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")])
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

    verdict = f"{len(top_picks)} strong opportunity(ies) found" if top_picks else "DO NOT BUY ANY STOCK TODAY — market conditions cautious"

    result = {
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

    _send_scan_notification(result.get("recommendations", []), result["verdict"], result["scanned"], result["universe_size"])
    return result

# ── Async scan endpoints ──────────────────────────────────────────────────
@app.post("/scan/start")
def start_scan(force_refresh: bool = False, background_tasks: BackgroundTasks = None):
    try:
        if force_refresh and _redis:
            try:
                _redis.delete(SCAN_UNIVERSE_KEY)
            except Exception:
                pass

        universe = _build_scan_universe()
        task_id = str(uuid.uuid4())
        background_tasks.add_task(run_scan_parallel, task_id, universe)
        return {"task_id": task_id}
    except Exception as e:
        logger.error(f"Scan start failed: {e}")
        raise HTTPException(status_code=500, detail=f"Scan failed: {str(e)}")

@app.get("/scan/status/{task_id}")
def get_scan_status(task_id: str):
    data = _redis_get(SCAN_TASK_PREFIX + task_id)
    if not data:
        raise HTTPException(status_code=404, detail="Task not found or expired")
    if data.get("status") == "running":
        processed = data.get("processed", 0)
        total = data.get("total", 0)
        elapsed = data.get("elapsed", 0)
        if processed > 0 and elapsed > 0:
            avg_time_per_stock = elapsed / processed
            remaining_stocks = total - processed
            estimated_remaining = round(remaining_stocks * avg_time_per_stock, 1)
            data["estimated_remaining"] = estimated_remaining
        else:
            data["estimated_remaining"] = None
    return data

# ── Watchlist-only scan ──────────────────────────────────────────────────
@app.get("/scan/watchlist")
def scan_watchlist():
    watchlist = _load_watchlist()
    if not watchlist:
        return {
            "scanned": 0,
            "universe_size": 0,
            "watchlist_size": 0,
            "recommendations": [],
            "watchlist_candidates": [],
            "verdict": "Watchlist is empty. Add some symbols first.",
            "market_mood": "Neutral",
            "market_stats": {
                "buy_signals": 0,
                "sell_signals": 0,
                "hold_signals": 0,
                "cautious": 0,
            },
            "all_results": [],
            "errors": [],
        }

    results = []
    errors = []

    with httpx.Client(timeout=180) as client:
        for symbol in watchlist:
            try:
                resp = client.get(f"{DECISION_URL}/decide/{symbol}")
                resp.raise_for_status()
                raw = resp.json()
                normalized = _normalize_decision_response(raw, symbol)

                if normalized.get("close") is None:
                    price = _fetch_price_from_quote(symbol)
                    if price is not None:
                        normalized["close"] = price
                        if normalized.get("support") is None:
                            normalized["support"] = round(price * 0.95, 2)
                        if normalized.get("resistance") is None:
                            normalized["resistance"] = round(price * 1.05, 2)

                _merge_fundamentals(normalized, symbol)

                if normalized.get("news_score") is None:
                    news = _fetch_news(symbol)
                    if news:
                        normalized["news_score"] = news.get("news_score")
                        reasons = normalized.get("reasons", {})
                        if news.get("reasons"):
                            reasons["news"] = news["reasons"]
                            normalized["reasons"] = reasons

                if normalized.get("event_risk") is False and not normalized.get("reasons", {}).get("event"):
                    events = _fetch_events(symbol)
                    if events and events.get("next_earnings_date"):
                        normalized["event_risk"] = True
                        reasons = normalized.get("reasons", {})
                        reasons["event"] = [f"Earnings due: {events['next_earnings_date']}"]
                        normalized["reasons"] = reasons

                if normalized.get("prediction_score") is None:
                    try:
                        pred_resp = client.get(f"{PREDICTION_URL}/predict/{symbol}", timeout=60)
                        if pred_resp.status_code == 200:
                            pred_data = pred_resp.json()
                            if pred_data.get("model_loaded"):
                                normalized["prediction_score"] = pred_data.get("prediction_score")
                                normalized["prediction_note"] = pred_data.get("note")
                    except Exception as e:
                        logger.warning(f"Prediction service lookup failed during watchlist scan for {symbol}: {e}")

                normalized["natural_language_summary"] = _generate_summary(normalized)
                results.append(normalized)
            except httpx.HTTPError as e:
                logger.warning("Watchlist scan skipped %s: %s", symbol, e)
                errors.append({"symbol": symbol, "error": str(e)})

    results.sort(key=lambda r: r.get("combined_score", 0), reverse=True)
    actionable = [r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")]
    top_picks = actionable[:5]

    buy_count = len([r for r in results if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")])
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

    verdict = f"{len(top_picks)} opportunity(ies) found" if top_picks else "No strong signals in your watchlist"

    result = {
        "scanned": len(results),
        "universe_size": len(watchlist),
        "watchlist_size": len(watchlist),
        "recommendations": top_picks,
        "watchlist_candidates": [],
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

    _send_scan_notification(result.get("recommendations", []), result["verdict"], result["scanned"], result["universe_size"])
    return result

# ── Market routes ────────────────────────────────────────────────────────────
@app.get("/market/top-gainers")
def market_top_gainers():
    data = _get_nifty50_data()
    sorted_data = sorted(data, key=lambda x: x["change_pct"], reverse=True)[:10]
    return {"data": sorted_data, "count": len(sorted_data)}

@app.get("/market/top-losers")
def market_top_losers():
    data = _get_nifty50_data()
    sorted_data = sorted(data, key=lambda x: x["change_pct"])[:10]
    return {"data": sorted_data, "count": len(sorted_data)}

@app.get("/market/most-active")
def market_most_active():
    data = _get_nifty50_data()
    sorted_data = sorted(data, key=lambda x: x["volume"], reverse=True)[:10]
    return {"data": sorted_data, "count": len(sorted_data)}

@app.get("/market/trending")
def market_trending():
    movers = _get_momentum_movers()
    news = _get_news_mentioned_symbols()
    trending = list(set(movers + news))
    trending_data = []
    for sym in trending[:10]:
        try:
            ticker = yf.Ticker(f"{sym}.NS")
            hist = ticker.history(period="1d")
            if not hist.empty:
                price = round(hist["Close"].iloc[-1], 2)
                change = round(hist["Close"].iloc[-1] - hist["Open"].iloc[-1], 2)
                change_pct = round(change / hist["Open"].iloc[-1] * 100, 2)
                trending_data.append({
                    "symbol": sym,
                    "price": price,
                    "change": change,
                    "change_pct": change_pct,
                })
        except:
            pass
    return {"data": trending_data, "count": len(trending_data)}

# ── Market Indices endpoint with caching and stale fallback ──────────────────
@app.get("/market/indices")
def get_market_indices():
    """
    Fetch real-time NIFTY 50 and SENSEX index values with point changes.
    Cached for 5 minutes to avoid rate limits. If yfinance fails, returns stale cache if available.
    """
    cached = _redis_get(INDICES_CACHE_KEY)
    if cached and isinstance(cached, dict):
        logger.info("Serving cached indices data")
        return cached

    try:
        nifty = yf.Ticker("^NSEI")
        sensex = yf.Ticker("^BSESN")
        nifty_hist = nifty.history(period="1d")
        sensex_hist = sensex.history(period="1d")
        if nifty_hist.empty or sensex_hist.empty:
            raise HTTPException(status_code=503, detail="Index data temporarily unavailable")

        nifty_close = nifty_hist['Close'].iloc[-1]
        nifty_open = nifty_hist['Open'].iloc[0]
        nifty_prev_close = nifty_hist['Close'].iloc[0] if len(nifty_hist) > 1 else nifty_open
        nifty_change = nifty_close - nifty_prev_close
        nifty_change_pct = (nifty_change / nifty_prev_close) * 100

        sensex_close = sensex_hist['Close'].iloc[-1]
        sensex_open = sensex_hist['Open'].iloc[0]
        sensex_prev_close = sensex_hist['Close'].iloc[0] if len(sensex_hist) > 1 else sensex_open
        sensex_change = sensex_close - sensex_prev_close
        sensex_change_pct = (sensex_change / sensex_prev_close) * 100

        avg_change_pct = (nifty_change_pct + sensex_change_pct) / 2
        if avg_change_pct > 0.3:
            mood = "BULLISH"
        elif avg_change_pct < -0.3:
            mood = "BEARISH"
        else:
            mood = "NEUTRAL"

        market_score = 50 + (avg_change_pct * 10)
        market_score = max(0, min(100, market_score))

        result = {
            "nifty": {
                "price": round(nifty_close, 2),
                "change": round(nifty_change, 2),
                "change_pct": round(nifty_change_pct, 2)
            },
            "sensex": {
                "price": round(sensex_close, 2),
                "change": round(sensex_change, 2),
                "change_pct": round(sensex_change_pct, 2)
            },
            "market_mood": mood,
            "market_score": round(market_score)
        }
        _redis_set(INDICES_CACHE_KEY, result, ttl=300)  # 5 minutes
        return result
    except Exception as e:
        logger.error(f"Error fetching indices: {e}")
        # If we have stale cache, return it
        stale = _redis_get(INDICES_CACHE_KEY)
        if stale and isinstance(stale, dict):
            logger.info("Returning stale cached indices data")
            stale["stale"] = True
            return stale
        # Otherwise raise a 500
        raise HTTPException(status_code=500, detail=str(e))

# ── Universe preview ──────────────────────────────────────────────────────
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

@app.post("/notifications/send-picks")
def send_picks_to_telegram(payload: dict):
    recs = payload.get("recommendations", [])
    if not recs:
        raise HTTPException(status_code=400, detail="No recommendations provided")

    _wake_notification_service()

    msg_type = payload.get("type", "top5")
    if msg_type == "top5":
        title = "📊 *Top 5 Picks from Market Scan*"
        picks = recs[:5]
    else:
        title = "📊 *All Actionable Stocks (BUY NOW / PREPARE TO BUY)*"
        picks = recs

    lines = [title, ""]
    for i, r in enumerate(picks, 1):
        symbol = r.get("symbol", "?")
        decision = r.get("decision", "UNKNOWN")
        score = r.get("combined_score", 0)
        close = r.get("close")
        target = r.get("target")
        stop = r.get("stop_loss")
        entry = r.get("entry_range", {})
        entry_low = entry.get("low")
        entry_high = entry.get("high")
        holding = r.get("holding_period", "N/A")

        lines.append(f"{i}. *{symbol}* – {decision} (Score: {score})")
        if close:
            lines.append(f"   Current: ₹{close:.2f}")
        if entry_low and entry_high:
            lines.append(f"   Entry: ₹{entry_low:.2f} – ₹{entry_high:.2f}")
        if target:
            upside = ((target - close) / close * 100) if close else 0
            lines.append(f"   Target: ₹{target:.2f} (+{upside:.1f}%)")
        if stop:
            lines.append(f"   Stop: ₹{stop:.2f}")
        if holding != "N/A":
            lines.append(f"   Hold: {holding}")
        lines.append("")

    message = "\n".join(lines)

    try:
        resp = httpx.post(
            f"{NOTIFICATION_URL}/notify",
            json={"title": "Market Scan Picks", "message": message, "channel": "telegram"},
            timeout=15,
        )
        resp.raise_for_status()
        return {"success": True, "sent": len(picks), "message": "Notification sent"}
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service failed: {e}")

# ============================================================================
# Training Service Proxy Routes
# ============================================================================

@app.get("/training/status")
async def training_status():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{TRAINING_URL}/model-status")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Training service unreachable: {str(e)}")

@app.post("/training/train")
async def trigger_training():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{TRAINING_URL}/train")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Training service unreachable: {str(e)}")

@app.get("/training/score/{symbol}")
async def get_training_score(symbol: str):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{TRAINING_URL}/training-score/{symbol}")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Training service unreachable: {str(e)}")

@app.api_route("/training/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def training_other_proxy(path: str, request: Request):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            target_url = f"{TRAINING_URL}/{path}"
            body = await request.body()
            headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "connection")}
            response = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
                params=request.query_params,
            )
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=dict(response.headers),
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Training service unreachable: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)