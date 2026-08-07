"""
Market Data Service
--------------------
Single responsibility: fetch raw market data (price history, quote, company info)
for Indian equities from free public sources (Yahoo Finance via yfinance) and
serve it over REST. No analysis logic lives here on purpose — this service is
a dumb, reliable data pipe so every other service can share one cache and one
rate-limit budget.

NSE tickers on Yahoo Finance use the ".NS" suffix (e.g. TCS.NS, RELIANCE.NS).
"""
import os
import time
import json
import logging
import math
import httpx
from datetime import datetime, timedelta
from typing import Optional

from upstash_redis import Redis
import yfinance as yf
from yfinance.exceptions import YFRateLimitError
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


def _safe(val, decimals=2):
    """Convert to float, round, handle NaN/Inf."""
    try:
        f = float(val)
        if math.isnan(f) or not math.isfinite(f):
            return None
        return round(f, decimals)
    except (TypeError, ValueError):
        return None


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("market-data-service")

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))  # 5 min

app = FastAPI(title="Stockky Market Data Service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Redis cache ----------
try:
    if UPSTASH_URL and UPSTASH_TOKEN:
        cache = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)
        cache.ping()
        logger.info("Connected to Upstash Redis")
    else:
        raise ValueError("Upstash credentials not set")
except Exception as e:
    logger.warning("Redis unavailable (%s). Running without cache.", e)
    cache = None


def _cache_get(key: str):
    if not cache:
        return None
    val = cache.get(key)
    return json.loads(val) if val else None


def _cache_set(key: str, value: dict, ttl: int = CACHE_TTL_SECONDS):
    if not cache:
        return
    cache.setex(key, ttl, json.dumps(value, default=str))


def normalize_symbol(symbol: str) -> str:
    """Accept 'TCS', 'TCS.NS', or 'tcs' and normalize to Yahoo's NSE format."""
    symbol = symbol.strip().upper()
    if not symbol.endswith(".NS") and not symbol.endswith(".BO"):
        symbol = f"{symbol}.NS"
    return symbol


def _fetch_info_with_retry(symbol: str, max_retries: int = 3, base_delay: int = 5):
    """
    Fetch ticker.info with exponential backoff on YFRateLimitError.
    Returns the info dict or raises HTTPException on failure.
    """
    ticker = yf.Ticker(symbol)
    for attempt in range(max_retries):
        try:
            return ticker.info
        except YFRateLimitError as e:
            wait = base_delay * (2 ** attempt)  # 5, 10, 20 seconds
            logger.warning(
                "Rate limit hit for %s. Retry %d/%d after %.0fs.",
                symbol, attempt+1, max_retries, wait
            )
            time.sleep(wait)
        except Exception as e:
            # Non-rate-limit error – raise immediately
            logger.exception("Unexpected error fetching fundamentals for %s", symbol)
            raise HTTPException(status_code=502, detail=f"Could not fetch fundamentals for {symbol}: {e}")
    # If all retries exhausted
    raise HTTPException(
        status_code=429,
        detail=f"Rate limited for {symbol}. Please try again later."
    )


class QuoteResponse(BaseModel):
    symbol: str
    name: Optional[str]
    price: Optional[float]
    previous_close: Optional[float]
    day_change_pct: Optional[float]
    day_high: Optional[float]
    day_low: Optional[float]
    volume: Optional[int]
    market_cap: Optional[float]
    pe_ratio: Optional[float]
    fetched_at: str


# ---------- ROOT ROUTE ----------
@app.get("/")
async def root():
    return {
        "service": "Stockky Market Data Service",
        "version": "0.1.0",
        "status": "running",
        "cache_enabled": bool(cache),
        "endpoints": {
            "/health": "GET – health check",
            "/quote/{symbol}": "GET – latest quote",
            "/history/{symbol}": "GET – OHLCV candles (period, interval)",
            "/fundamentals/{symbol}": "GET – raw fundamental data",
            "/docs": "Swagger UI documentation",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "market-data-service", "cache": bool(cache)}


@app.get("/quote/{symbol}", response_model=QuoteResponse)
def get_quote(symbol: str):
    """Latest quote snapshot for a single stock."""
    sym = normalize_symbol(symbol)
    cache_key = f"quote:{sym}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    ticker = yf.Ticker(sym)
    ticker._tz = "Asia/Kolkata"
    try:
            info = ticker.info
    except Exception:
            info = {}
    if not info:
            raise HTTPException(status_code=404, detail=f"No fundamentals for {sym}")
    try:
            full_info = ticker.info
    except Exception:
            pass  # fast_info is enough to still return a usable quote

    price = getattr(info, "last_price", None)
    prev_close = getattr(info, "previous_close", None)
    change_pct = None
    if price and prev_close:
            change_pct = round(((price - prev_close) / prev_close) * 100, 2)

    result = {
            "symbol": sym,
            "name": full_info.get("longName") or full_info.get("shortName") or sym,
            "price": price,
            "previous_close": prev_close,
            "day_change_pct": change_pct,
            "day_high": getattr(info, "day_high", None),
            "day_low": getattr(info, "day_low", None),
            "volume": getattr(info, "last_volume", None),
            "market_cap": getattr(info, "market_cap", None),
            "pe_ratio": full_info.get("trailingPE"),
            "fetched_at": datetime.utcnow().isoformat(),
        }
    _cache_set(cache_key, result)
    return result

@app.get("/history/{symbol}")
def get_history(
    symbol: str,
    period: str = Query("6mo", description="1mo, 3mo, 6mo, 1y, 2y, 5y"),
    interval: str = Query("1d", description="1d, 1wk, 1h"),
):
    """OHLCV candle history, used by the Technical Analysis Service."""
    sym = normalize_symbol(symbol)
    cache_key = f"history:{sym}:{period}:{interval}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        ticker = yf.Ticker(sym)
        ticker._tz = "Asia/Kolkata"
        df = ticker.history(period=period, interval=interval, auto_adjust=True)

        if df.empty:
            raise HTTPException(status_code=404, detail=f"No history found for {sym}")

        candles = []
        for idx, row in df.iterrows():
            # Skip rows where Close is NaN (bad data)
            if _safe(row["Close"]) is None:
                continue
            candles.append({
                "date": idx.strftime("%Y-%m-%d %H:%M"),
                "open": _safe(row["Open"]),
                "high": _safe(row["High"]),
                "low": _safe(row["Low"]),
                "close": _safe(row["Close"]),
                "volume": int(row["Volume"]) if math.isfinite(float(row["Volume"])) else 0,
            })

        if not candles:
            raise HTTPException(status_code=404, detail=f"No valid candles for {sym}")

        result = {"symbol": sym, "period": period, "interval": interval, "candles": candles}
        _cache_set(cache_key, result, ttl=900)  # history changes less often
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to fetch history for %s", sym)
        raise HTTPException(status_code=502, detail=f"Could not fetch history for {sym}: {e}")


@app.get("/fundamentals/{symbol}")
def get_fundamentals_raw(symbol: str):
    """Fundamental data built from fast_info + quote + dividends history.
    Yahoo's full .info endpoint is blocked from cloud environments (403),
    so we assemble what we can from the endpoints that do work."""
    sym = normalize_symbol(symbol)
    cache_key = f"fundamentals:{sym}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        ticker = yf.Ticker(sym)
        ticker._tz = "Asia/Kolkata"

        # fast_info — price-level data, works from cloud
        fi = ticker.fast_info

        def _safe_fi(attr):
            try:
                v = getattr(fi, attr, None)
                return None if (v is None or (isinstance(v, float) and (v != v))) else v
            except Exception:
                return None

        last_price   = _safe_fi("last_price")
        market_cap   = _safe_fi("market_cap")
        year_high    = _safe_fi("year_high")
        year_low     = _safe_fi("year_low")
        year_change  = _safe_fi("year_change")
        fifty_day    = _safe_fi("fifty_day_average")
        two_hundred  = _safe_fi("two_hundred_day_average")

        # Dividends — works from cloud via history endpoint
        dividend_yield = None
        try:
            divs = ticker.dividends
            if divs is not None and not divs.empty and last_price:
                annual_div = float(divs.tail(4).sum())  # trailing 4 quarters
                dividend_yield = round(annual_div / last_price * 100, 2)
        except Exception:
            pass

        # PE ratio — already in quote endpoint, fetch from there
        pe_ratio = None
        try:
            q_resp = httpx.get(f"http://localhost:{os.getenv('PORT', '8001')}/quote/{symbol}", timeout=10)
            if q_resp.status_code == 200:
                pe_ratio = q_resp.json().get("pe_ratio")
        except Exception:
            pass

        # Price momentum as a proxy for earnings growth
        momentum = None
        if year_change is not None:
            momentum = round(year_change * 100, 2)

        # 52-week range position (0-100%) as valuation proxy
        range_position = None
        if year_high and year_low and last_price and year_high != year_low:
            range_position = round((last_price - year_low) / (year_high - year_low) * 100, 1)

        result = {
            "symbol": sym,
            "pe_ratio": pe_ratio,
            "forward_pe": None,
            "market_cap": market_cap,
            "dividend_yield": dividend_yield,
            "year_change_pct": momentum,
            "year_high": year_high,
            "year_low": year_low,
            "fifty_day_average": fifty_day,
            "two_hundred_day_average": two_hundred,
            "range_position_pct": range_position,
            # Fields not available from cloud — honest nulls
            "revenue_growth": None,
            "earnings_growth": None,
            "eps": None,
            "roe": None,
            "debt_to_equity": None,
            "free_cashflow": None,
            "profit_margins": None,
            "held_percent_insiders": None,
            "held_percent_institutions": None,
            "price_to_book": None,
            "sector": None,
            "industry": None,
        }

        _cache_set(cache_key, result, ttl=3600)
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to fetch fundamentals for %s", sym)
        raise HTTPException(status_code=502, detail=f"Could not fetch fundamentals for {sym}: {e}")

if __name__ == "__main__":
    import uvicorn

    # Render provides $PORT – always use it
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)