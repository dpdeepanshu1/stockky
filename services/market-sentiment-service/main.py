"""
Market Sentiment Service
Responsibility: Fetch Indian index data (NIFTY 50, SENSEX) and compute a
normalized market sentiment score and classification.

v0.4.0 – batch fetch, request locking, stale cache fallback, never 503.
"""
import os
import logging
import time
from datetime import datetime
from typing import Dict, Optional, Any, List
import asyncio

import yfinance as yf
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("market-sentiment-service")

# --- Configuration ---
# Only two main indices to reduce API calls
INDEX_SYMBOLS: Dict[str, str] = {
    "NIFTY 50": "^NSEI",
    "SENSEX": "^BSESN",
}

# --- In-memory cache ---
_cache: Dict[str, Any] = {
    "data": None,
    "timestamp": None,
    "ttl_seconds": 120,  # 2 minutes
    "lock": asyncio.Lock(),
}

app = FastAPI(title="Stockky Market Sentiment Service", version="0.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Data Models ---
class IndexData(BaseModel):
    symbol: str
    name: str
    current: Optional[float]
    previous_close: Optional[float]
    change: Optional[float]
    change_percent: Optional[float]
    high: Optional[float]
    low: Optional[float]
    volume: Optional[int]
    timestamp: datetime

class MarketSentimentResponse(BaseModel):
    timestamp: datetime
    indices: Dict[str, IndexData]
    market_score: int  # 0-100
    classification: str
    trend: Optional[str]
    momentum: Optional[str]
    breadth: Optional[str]
    volatility: Optional[str]
    cached: bool = False
    stale: bool = False

# --- Helper Functions ---
def _safe_float(val):
    try:
        f = float(val)
        if np.isnan(f) or not np.isfinite(f):
            return None
        return round(f, 2)
    except (TypeError, ValueError):
        return None

def _safe_int(val):
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None

def fetch_indices_batch(symbols: Dict[str, str]) -> Dict[str, IndexData]:
    """Fetch all indices in a single batch using yf.download."""
    result = {}
    if not symbols:
        return result

    yf_symbols = list(symbols.values())
    max_retries = 3
    data = None
    for attempt in range(max_retries):
        try:
            data = yf.download(
                tickers=yf_symbols,
                period="2d",
                interval="1d",
                group_by='ticker',
                auto_adjust=True,
                threads=False,
                progress=False
            )
            logger.info(f"Batch download success: {len(data)} tickers")
            break
        except Exception as e:
            if attempt < max_retries - 1:
                wait = (2 ** attempt) + 1
                logger.warning(f"Batch download attempt {attempt+1} failed: {e}, retrying in {wait}s")
                time.sleep(wait)
                continue
            else:
                logger.error(f"Batch download failed after {max_retries} retries: {e}")
                return result

    if data is None:
        return result

    # Process each symbol
    for name, sym in symbols.items():
        try:
            if sym not in data.columns or data[sym].empty:
                logger.warning(f"No data for {name} ({sym})")
                continue
            df = data[sym]
            if len(df) >= 2:
                current = _safe_float(df['Close'].iloc[-1])
                prev_close = _safe_float(df['Close'].iloc[-2])
                change = _safe_float(current - prev_close) if current and prev_close else None
                change_pct = _safe_float((change / prev_close) * 100) if change and prev_close else None
                high = _safe_float(df['High'].iloc[-1])
                low = _safe_float(df['Low'].iloc[-1])
                volume = _safe_int(df['Volume'].iloc[-1])
                result[name] = IndexData(
                    symbol=sym,
                    name=name,
                    current=current,
                    previous_close=prev_close,
                    change=change,
                    change_percent=change_pct,
                    high=high,
                    low=low,
                    volume=volume,
                    timestamp=datetime.now()
                )
            else:
                logger.warning(f"Insufficient data for {name} ({sym})")
        except Exception as e:
            logger.error(f"Error processing {name} ({sym}): {e}")

    return result

def compute_market_score(indices_data: Dict[str, IndexData]) -> int:
    """Compute score from average change of NIFTY 50 and SENSEX."""
    if not indices_data:
        return 50

    changes = [d.change_percent for d in indices_data.values() if d.change_percent is not None]
    if not changes:
        return 50

    avg_change = np.mean(changes)
    # Map -0.5% -> 0, 0% -> 50, +0.5% -> 100
    score = min(100, max(0, 50 + (avg_change / 0.005)))
    return int(round(score))

def classify_sentiment(score: int) -> str:
    if score >= 75:
        return "STRONGLY BULLISH"
    elif score >= 55:
        return "BULLISH"
    elif score >= 45:
        return "NEUTRAL"
    elif score >= 25:
        return "BEARISH"
    else:
        return "STRONGLY BEARISH"

# --- API Endpoints ---
@app.get("/sentiment", response_model=MarketSentimentResponse)
async def get_market_sentiment(force_refresh: bool = False):
    now = datetime.now()

    # Check cache
    if not force_refresh and _cache["data"] is not None:
        cache_age = (now - _cache["timestamp"]).total_seconds() if _cache["timestamp"] else 9999
        if cache_age < _cache["ttl_seconds"]:
            logger.info("Returning cached market sentiment")
            cached_response = _cache["data"].copy()
            cached_response["cached"] = True
            return MarketSentimentResponse(**cached_response)

    # Acquire lock to prevent concurrent fetches
    async with _cache["lock"]:
        # Double-check cache after acquiring lock
        if not force_refresh and _cache["data"] is not None:
            cache_age = (now - _cache["timestamp"]).total_seconds() if _cache["timestamp"] else 9999
            if cache_age < _cache["ttl_seconds"]:
                cached_response = _cache["data"].copy()
                cached_response["cached"] = True
                return MarketSentimentResponse(**cached_response)

        logger.info("Fetching fresh market sentiment data")
        indices_data = fetch_indices_batch(INDEX_SYMBOLS)

        if not indices_data:
            # Return stale cache if available
            if _cache["data"] is not None:
                logger.warning("No fresh data, returning stale cache")
                stale_response = _cache["data"].copy()
                stale_response["cached"] = True
                stale_response["stale"] = True
                return MarketSentimentResponse(**stale_response)
            else:
                # No cache – return neutral fallback
                logger.error("No index data available, returning neutral fallback")
                fallback = {
                    "timestamp": now,
                    "indices": {},
                    "market_score": 50,
                    "classification": "NEUTRAL",
                    "trend": "Neutral",
                    "momentum": "Moderate",
                    "breadth": "Mixed",
                    "volatility": "Normal",
                    "cached": False,
                    "stale": True,
                }
                _cache["data"] = fallback
                _cache["timestamp"] = now
                return MarketSentimentResponse(**fallback)

        score = compute_market_score(indices_data)
        classification = classify_sentiment(score)

        # Derive labels
        trend = "Bullish" if score > 55 else "Bearish" if score < 45 else "Neutral"
        momentum = "Strong" if score > 65 else "Weak" if score < 35 else "Moderate"
        up_count = sum(1 for d in indices_data.values() if d.change and d.change > 0)
        breadth_pct = (up_count / len(indices_data)) * 100 if indices_data else 50
        breadth = "Positive" if breadth_pct > 60 else "Negative" if breadth_pct < 40 else "Mixed"
        volatility = "Normal" if 35 < score < 65 else "High"

        response_data = {
            "timestamp": now,
            "indices": indices_data,
            "market_score": score,
            "classification": classification,
            "trend": trend,
            "momentum": momentum,
            "breadth": breadth,
            "volatility": volatility,
            "cached": False,
            "stale": False,
        }

        _cache["data"] = response_data
        _cache["timestamp"] = now

        return MarketSentimentResponse(**response_data)

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "market-sentiment-service"}

@app.get("/")
async def root():
    return {
        "service": "Stockky Market Sentiment Service",
        "version": "0.4.0",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/sentiment": "GET – current market sentiment (cached 120s)",
            "/sentiment?force_refresh=true": "GET – force refresh",
        },
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8009))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=True)