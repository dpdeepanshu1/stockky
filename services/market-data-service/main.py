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


def _safe_int(val):
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _compute_growth(current, previous):
    """Compute percentage growth from previous to current."""
    if previous is None or previous == 0:
        return None
    return ((current - previous) / previous) * 100


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
    """Fundamental data built from info, financials, balance sheet, cashflow.
    Fetches full info (may work), falls back to financials if info fails.
    """
    sym = normalize_symbol(symbol)
    cache_key = f"fundamentals:{sym}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        ticker = yf.Ticker(sym)
        ticker._tz = "Asia/Kolkata"

        # 1. Try to get full info (may work from some IPs)
        try:
            info = ticker.info
        except Exception:
            info = {}

        # 2. Get financials, balance sheet, cashflow
        financials = ticker.financials
        balance = ticker.balance_sheet
        cashflow = ticker.cashflow

        # Helper to safely get info key
        def _safe_info(key):
            val = info.get(key)
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        # --- Extract metrics from info or financials ---

        # Revenue growth (from financials)
        revenue_growth = None
        if not financials.empty and "Total Revenue" in financials.index:
            rev_series = financials.loc["Total Revenue"]
            if len(rev_series) >= 2:
                current_rev = rev_series.iloc[0]
                prev_rev = rev_series.iloc[1]
                revenue_growth = _compute_growth(current_rev, prev_rev)

        # Earnings growth (Net Income)
        earnings_growth = None
        if not financials.empty and "Net Income" in financials.index:
            earnings_series = financials.loc["Net Income"]
            if len(earnings_series) >= 2:
                current_earn = earnings_series.iloc[0]
                prev_earn = earnings_series.iloc[1]
                earnings_growth = _compute_growth(current_earn, prev_earn)

        # ROE (Net Income / Shareholders' Equity)
        roe = None
        if "returnOnEquity" in info:
            roe = _safe_info("returnOnEquity") * 100 if _safe_info("returnOnEquity") else None
        elif not balance.empty and "Total Equity Gross Minority Interest" in balance.index:
            equity = balance.loc["Total Equity Gross Minority Interest"].iloc[0]
            if not financials.empty and "Net Income" in financials.index:
                net_income = financials.loc["Net Income"].iloc[0]
                if equity != 0:
                    roe = (net_income / equity) * 100

        # Debt to Equity
        debt_to_equity = None
        if "debtToEquity" in info:
            debt_to_equity = _safe_info("debtToEquity")
        elif not balance.empty and "Total Debt" in balance.index and "Total Equity Gross Minority Interest" in balance.index:
            total_debt = balance.loc["Total Debt"].iloc[0]
            equity = balance.loc["Total Equity Gross Minority Interest"].iloc[0]
            if equity != 0:
                debt_to_equity = total_debt / equity

        # Free Cash Flow
        free_cashflow = None
        if "freeCashflow" in info:
            free_cashflow = _safe_info("freeCashflow")
        elif not cashflow.empty and "Free Cash Flow" in cashflow.index:
            free_cashflow = cashflow.loc["Free Cash Flow"].iloc[0]

        # Profit Margins (Net Margin)
        profit_margins = None
        if "profitMargins" in info:
            profit_margins = _safe_info("profitMargins") * 100
        else:
            # Compute from financials
            if not financials.empty and "Net Income" in financials.index and "Total Revenue" in financials.index:
                net_income = financials.loc["Net Income"].iloc[0]
                revenue = financials.loc["Total Revenue"].iloc[0]
                if revenue != 0:
                    profit_margins = (net_income / revenue) * 100

        # Institutional Holding
        held_percent_institutions = None
        if "heldPercentInstitutions" in info:
            held_percent_institutions = _safe_info("heldPercentInstitutions") * 100
        elif "institutionalPercent" in info:
            held_percent_institutions = _safe_info("institutionalPercent") * 100

        # PE Ratio
        pe_ratio = None
        if "trailingPE" in info:
            pe_ratio = _safe_info("trailingPE")
        elif "peRatio" in info:
            pe_ratio = _safe_info("peRatio")

        # Forward PE
        forward_pe = None
        if "forwardPE" in info:
            forward_pe = _safe_info("forwardPE")

        # EPS
        eps = None
        if "trailingEps" in info:
            eps = _safe_info("trailingEps")
        elif "eps" in info:
            eps = _safe_info("eps")

        # Price to Book
        price_to_book = None
        if "priceToBook" in info:
            price_to_book = _safe_info("priceToBook")

        # Market Cap
        market_cap = _safe_info("marketCap")

        # Dividend Yield
        dividend_yield = None
        if "dividendYield" in info:
            dividend_yield = _safe_info("dividendYield") * 100
        else:
            # Compute from dividends
            try:
                divs = ticker.dividends
                if divs is not None and not divs.empty:
                    last_price = _safe_info("regularMarketPrice") or _safe_info("last_price")
                    if last_price:
                        annual_div = float(divs.tail(4).sum())
                        dividend_yield = round(annual_div / last_price * 100, 2)
            except Exception:
                pass

        # Year high/low, averages
        year_high = _safe_info("fiftyTwoWeekHigh")
        year_low = _safe_info("fiftyTwoWeekLow")
        fifty_day_average = _safe_info("fiftyDayAverage")
        two_hundred_day_average = _safe_info("twoHundredDayAverage")
        year_change_pct = _safe_info("52WeekChange")
        if year_change_pct is not None:
            year_change_pct = year_change_pct * 100

        # Range position
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

        _cache_set(cache_key, result, ttl=3600)
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to fetch fundamentals for %s", sym)
        raise HTTPException(status_code=502, detail=f"Could not fetch fundamentals for {sym}: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)