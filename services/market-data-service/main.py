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
import random
from datetime import datetime
from typing import Optional

import requests
import yfinance as yf
from upstash_redis import Redis
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# --- Patch yfinance session with a proper User-Agent ---
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
})

try:
    yf.set_session(session)
except AttributeError:
    try:
        yf.shared._session = session
    except AttributeError:
        pass

try:
    yf.set_tz_cache_location("/tmp/yfinance_tz")
except AttributeError:
    pass

def _safe(val, decimals=2):
    try:
        f = float(val)
        if math.isnan(f) or not math.isfinite(f):
            return None
        return round(f, decimals)
    except (TypeError, ValueError):
        return None

def _safe_int(val):
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None

def _compute_growth(current, previous):
    if previous is None or previous == 0:
        return None
    return ((current - previous) / previous) * 100

def _with_retry(func, max_retries=3, base_delay=2):
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            # Full jitter: avoids every concurrent request retrying in
            # lockstep, which itself looks like a burst to Yahoo's rate
            # limiter and makes the block worse, not better.
            wait = random.uniform(0, base_delay * (2 ** attempt))
            logging.warning(f"Retry {attempt+1}/{max_retries} after {wait:.1f}s: {e}")
            time.sleep(wait)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("market-data-service")

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))

app = FastAPI(title="Stockky Market Data Service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal error: {str(exc)}"},
        headers={"Access-Control-Allow-Origin": "*"}
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

# Separate from the normal short-TTL cache: this one never expires on its
# own (30-day rolling TTL, refreshed every time we get a genuinely good
# response). Fundamentals change slowly (quarterly, really), so once we've
# successfully fetched a symbol, there's no good reason a temporary Yahoo
# rate-limit block should make that data disappear for users — we'd rather
# serve data that's a few days stale than an empty "no data" response.
FALLBACK_TTL_SECONDS = 30 * 24 * 60 * 60

def _fallback_get(key: str):
    if not cache:
        return None
    val = cache.get(f"fallback:{key}")
    return json.loads(val) if val else None

def _fallback_set(key: str, value: dict):
    if not cache:
        return
    cache.setex(f"fallback:{key}", FALLBACK_TTL_SECONDS, json.dumps(value, default=str))

def normalize_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not symbol.endswith(".NS") and not symbol.endswith(".BO"):
        symbol = f"{symbol}.NS"
    return symbol

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
        raise HTTPException(status_code=404, detail=f"No data for {sym}")

    price = info.get("regularMarketPrice") or info.get("last_price")
    prev_close = info.get("previousClose")
    change_pct = None
    if price and prev_close:
        change_pct = round(((price - prev_close) / prev_close) * 100, 2)

    result = {
        "symbol": sym,
        "name": info.get("longName") or info.get("shortName") or sym,
        "price": price,
        "previous_close": prev_close,
        "day_change_pct": change_pct,
        "day_high": info.get("dayHigh"),
        "day_low": info.get("dayLow"),
        "volume": info.get("volume"),
        "market_cap": info.get("marketCap"),
        "pe_ratio": info.get("trailingPE"),
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
        _cache_set(cache_key, result, ttl=900)
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to fetch history for %s", sym)
        raise HTTPException(status_code=502, detail=f"Could not fetch history for {sym}: {e}")

@app.get("/fundamentals/{symbol}")
def get_fundamentals_raw(symbol: str):
    sym = normalize_symbol(symbol)
    cache_key = f"fundamentals:{sym}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        ticker = yf.Ticker(sym)
        ticker._tz = "Asia/Kolkata"

        info = {}
        try:
            info = _with_retry(lambda: ticker.info, max_retries=4, base_delay=2)
        except Exception as e:
            logger.warning(f"Could not fetch info for {sym}: {e}")

        financials = None
        balance = None
        cashflow = None
        try:
            financials = _with_retry(lambda: ticker.financials, max_retries=2, base_delay=1)
        except Exception as e:
            logger.warning(f"Could not fetch financials for {sym}: {e}")
        try:
            balance = _with_retry(lambda: ticker.balance_sheet, max_retries=2, base_delay=1)
        except Exception as e:
            logger.warning(f"Could not fetch balance sheet for {sym}: {e}")
        try:
            cashflow = _with_retry(lambda: ticker.cashflow, max_retries=2, base_delay=1)
        except Exception as e:
            logger.warning(f"Could not fetch cashflow for {sym}: {e}")

        def _safe_info(key):
            val = info.get(key)
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        financials_available = financials is not None and not financials.empty
        balance_available = balance is not None and not balance.empty
        cashflow_available = cashflow is not None and not cashflow.empty

        # Revenue growth
        revenue_growth = None
        if financials_available and "Total Revenue" in financials.index:
            rev_series = financials.loc["Total Revenue"]
            if len(rev_series) >= 2:
                current_rev = rev_series.iloc[0]
                prev_rev = rev_series.iloc[1]
                revenue_growth = _compute_growth(current_rev, prev_rev)

        earnings_growth = None
        if financials_available and "Net Income" in financials.index:
            earnings_series = financials.loc["Net Income"]
            if len(earnings_series) >= 2:
                current_earn = earnings_series.iloc[0]
                prev_earn = earnings_series.iloc[1]
                earnings_growth = _compute_growth(current_earn, prev_earn)

        roe = None
        if "returnOnEquity" in info:
            roe = _safe_info("returnOnEquity") * 100 if _safe_info("returnOnEquity") else None
        elif balance_available and "Total Equity Gross Minority Interest" in balance.index:
            equity = balance.loc["Total Equity Gross Minority Interest"].iloc[0]
            if financials_available and "Net Income" in financials.index:
                net_income = financials.loc["Net Income"].iloc[0]
                if equity != 0:
                    roe = (net_income / equity) * 100

        debt_to_equity = None
        if "debtToEquity" in info:
            debt_to_equity = _safe_info("debtToEquity")
        elif balance_available and "Total Debt" in balance.index and "Total Equity Gross Minority Interest" in balance.index:
            total_debt = balance.loc["Total Debt"].iloc[0]
            equity = balance.loc["Total Equity Gross Minority Interest"].iloc[0]
            if equity != 0:
                debt_to_equity = total_debt / equity

        free_cashflow = None
        if "freeCashflow" in info:
            free_cashflow = _safe_info("freeCashflow")
        elif cashflow_available and "Free Cash Flow" in cashflow.index:
            free_cashflow = cashflow.loc["Free Cash Flow"].iloc[0]

        profit_margins = None
        if "profitMargins" in info:
            profit_margins = _safe_info("profitMargins") * 100
        else:
            if financials_available and "Net Income" in financials.index and "Total Revenue" in financials.index:
                net_income = financials.loc["Net Income"].iloc[0]
                revenue = financials.loc["Total Revenue"].iloc[0]
                if revenue != 0:
                    profit_margins = (net_income / revenue) * 100

        held_percent_institutions = None
        if "heldPercentInstitutions" in info:
            held_percent_institutions = _safe_info("heldPercentInstitutions") * 100
        elif "institutionalPercent" in info:
            held_percent_institutions = _safe_info("institutionalPercent") * 100

        pe_ratio = None
        if "trailingPE" in info:
            pe_ratio = _safe_info("trailingPE")
        elif "peRatio" in info:
            pe_ratio = _safe_info("peRatio")

        forward_pe = None
        if "forwardPE" in info:
            forward_pe = _safe_info("forwardPE")

        eps = None
        if "trailingEps" in info:
            eps = _safe_info("trailingEps")
        elif "eps" in info:
            eps = _safe_info("eps")

        price_to_book = None
        if "priceToBook" in info:
            price_to_book = _safe_info("priceToBook")

        market_cap = _safe_info("marketCap")

        dividend_yield = None
        if "dividendYield" in info:
            dividend_yield = _safe_info("dividendYield") * 100
        else:
            try:
                divs = ticker.dividends
                if divs is not None and not divs.empty:
                    last_price = _safe_info("regularMarketPrice") or _safe_info("last_price")
                    if last_price:
                        annual_div = float(divs.tail(4).sum())
                        dividend_yield = round(annual_div / last_price * 100, 2)
            except Exception:
                pass

        year_high = _safe_info("fiftyTwoWeekHigh")
        year_low = _safe_info("fiftyTwoWeekLow")
        fifty_day_average = _safe_info("fiftyDayAverage")
        two_hundred_day_average = _safe_info("twoHundredDayAverage")
        year_change_pct = _safe_info("52WeekChange")
        if year_change_pct is not None:
            year_change_pct = year_change_pct * 100

        range_position = None
        if year_high and year_low:
            last_price = _safe_info("regularMarketPrice") or _safe_info("last_price")
            if last_price and year_high != year_low:
                range_position = round((last_price - year_low) / (year_high - year_low) * 100, 1)

        sector = info.get("sector")
        industry = info.get("industry")

        result = {
            "symbol": sym,
            "pe_ratio": pe_ratio,
            "forward_pe": forward_pe,
            "market_cap": market_cap,
            "dividend_yield": dividend_yield,
            "year_change_pct": year_change_pct,
            "year_high": year_high,
            "year_low": year_low,
            "fifty_day_average": fifty_day_average,
            "two_hundred_day_average": two_hundred_day_average,
            "range_position_pct": range_position,
            "revenue_growth": revenue_growth,
            "earnings_growth": earnings_growth,
            "eps": eps,
            "roe": roe,
            "debt_to_equity": debt_to_equity,
            "free_cashflow": free_cashflow,
            "profit_margins": profit_margins,
            "held_percent_insiders": _safe_info("heldPercentInsiders") * 100 if _safe_info("heldPercentInsiders") else None,
            "held_percent_institutions": held_percent_institutions,
            "price_to_book": price_to_book,
            "sector": sector,
            "industry": industry,
        }

        logger.info(f"Fundamentals for {sym}: PE={pe_ratio}, ROE={roe}, Revenue growth={revenue_growth}")

        # Fundamentals barely move day to day — cache the "fast path" for a
        # full day, not 5 minutes, to keep us well clear of Yahoo's rate
        # limiter during normal usage.
        _cache_set(cache_key, result, ttl=86400)

        meaningful_fields = [
            revenue_growth, earnings_growth, roe, debt_to_equity,
            free_cashflow, profit_margins, pe_ratio,
        ]
        if any(v is not None for v in meaningful_fields):
            # Only refresh the fallback when we actually got real numbers —
            # never overwrite good fallback data with an empty response.
            _fallback_set(cache_key, result)
        else:
            stale = _fallback_get(cache_key)
            if stale:
                logger.info("Live fetch for %s came back empty; serving last-known-good fallback", sym)
                stale = dict(stale)
                stale["stale"] = True
                _cache_set(cache_key, stale, ttl=1800)  # short TTL: keep retrying the live path soon
                return stale

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to fetch fundamentals for %s", sym)
        stale = _fallback_get(cache_key)
        if stale:
            logger.info("Live fetch for %s failed (%s); serving last-known-good fallback", sym, e)
            stale = dict(stale)
            stale["stale"] = True
            _cache_set(cache_key, stale, ttl=1800)
            return stale
        raise HTTPException(status_code=502, detail=f"Could not fetch fundamentals for {sym}: {e}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)