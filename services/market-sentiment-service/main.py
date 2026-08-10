"""
Market Sentiment Service
Responsibility: Fetch Indian index data (NIFTY 50, SENSEX, etc.) and compute a
normalized market sentiment score and classification.

v0.3.0 – improved scoring sensitivity, weighted towards main indices.
"""
import os
import logging
import time
from datetime import datetime
from typing import Dict, Optional, Any, List

import yfinance as yf
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("market-sentiment-service")

# --- Configuration ---
INDEX_SYMBOLS: Dict[str, str] = {
    "NIFTY 50": "^NSEI",
    "SENSEX": "^BSESN",
    "NIFTY Bank": "^NSEBANK",
    "NIFTY IT": "^CNXIT",
    "NIFTY Auto": "^CNXAUTO",
    "NIFTY Pharma": "^CNXPHARMA",
    "NIFTY Metal": "^CNXMETAL",
    "NIFTY Energy": "^CNXENERGY",
    "NIFTY FMCG": "^CNXFMCG",
}

# --- In-memory cache ---
_cache: Dict[str, Any] = {
    "data": None,
    "timestamp": None,
    "ttl_seconds": 120  # 2 minutes
}

app = FastAPI(title="Stockky Market Sentiment Service", version="0.3.0")
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

def fetch_index_data_with_retry(symbol: str, name: str, max_retries=3) -> Optional[IndexData]:
    """Fetch index data with retry on 429 errors."""
    for attempt in range(max_retries):
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            if info and info.get('regularMarketPrice') is not None:
                current = _safe_float(info.get('regularMarketPrice'))
                previous_close = _safe_float(info.get('previousClose'))
                change = _safe_float(info.get('regularMarketChange'))
                change_percent = _safe_float(info.get('regularMarketChangePercent'))
                high = _safe_float(info.get('regularMarketDayHigh'))
                low = _safe_float(info.get('regularMarketDayLow'))
                volume = _safe_int(info.get('regularMarketVolume'))
                return IndexData(
                    symbol=symbol,
                    name=name,
                    current=current,
                    previous_close=previous_close,
                    change=change,
                    change_percent=change_percent,
                    high=high,
                    low=low,
                    volume=volume,
                    timestamp=datetime.now()
                )
            else:
                hist = ticker.history(period="2d")
                if len(hist) >= 2:
                    current = _safe_float(hist['Close'].iloc[-1])
                    previous_close = _safe_float(hist['Close'].iloc[-2])
                    change = current - previous_close if current and previous_close else None
                    change_percent = (change / previous_close * 100) if change and previous_close else None
                    high = _safe_float(hist['High'].iloc[-1])
                    low = _safe_float(hist['Low'].iloc[-1])
                    volume = _safe_int(hist['Volume'].iloc[-1])
                    return IndexData(
                        symbol=symbol,
                        name=name,
                        current=current,
                        previous_close=previous_close,
                        change=change,
                        change_percent=change_percent,
                        high=high,
                        low=low,
                        volume=volume,
                        timestamp=datetime.now()
                    )
                else:
                    logger.warning(f"Insufficient history for {name} ({symbol})")
                    return None
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "Too Many Requests" in error_str:
                if attempt < max_retries - 1:
                    wait = (2 ** attempt) + 0.5
                    logger.warning(f"Rate limit for {name} (attempt {attempt+1}), retrying in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                else:
                    logger.error(f"Rate limit persisted for {name} after {max_retries} retries")
                    return None
            else:
                logger.error(f"Error fetching data for {symbol} ({name}): {e}")
                return None
    return None

def compute_market_score(indices_data: Dict[str, IndexData]) -> int:
    """
    Compute a normalized market score (0-100) with improved sensitivity.
    - Uses weighted average: NIFTY 50 and SENSEX have higher weight.
    - Momentum mapping: -0.5% -> 0, 0% -> 50, +0.5% -> 100.
    - Trend and volatility components are also more sensitive.
    """
    weights = {
        "NIFTY 50": 0.35,
        "SENSEX": 0.30,
        "NIFTY Bank": 0.10,
        "NIFTY IT": 0.05,
        "NIFTY Auto": 0.05,
        "NIFTY Pharma": 0.05,
        "NIFTY Metal": 0.03,
        "NIFTY Energy": 0.04,
        "NIFTY FMCG": 0.03,
    }
    # Normalize weights for available indices
    available = [name for name in indices_data if name in weights]
    if not available:
        return 50
    total_weight = sum(weights.get(name, 0) for name in available)
    if total_weight == 0:
        return 50
    # Normalize
    norm_weights = {name: weights.get(name, 0) / total_weight for name in available}

    scores = []
    for name, data in indices_data.items():
        if name not in norm_weights:
            continue
        w = norm_weights[name]
        # 1. Momentum score from change_percent (sensitive: -0.5% -> 0, +0.5% -> 100)
        mom_score = 50
        if data.change_percent is not None:
            # Map -0.5% to 0, 0% to 50, +0.5% to 100
            mom_score = min(100, max(0, 50 + (data.change_percent / 0.005)))  # 0.5% = 0.005
        # 2. Trend: compare to 50-day SMA
        trend_score = 50
        if data.current is not None:
            try:
                ticker = yf.Ticker(INDEX_SYMBOLS[name])
                hist = ticker.history(period="3mo")
                if len(hist) >= 50:
                    sma_50 = hist['Close'].rolling(50).mean().iloc[-1]
                    if sma_50:
                        deviation = (data.current - sma_50) / sma_50 * 100  # percentage
                        # Map -1% -> 0, 0% -> 50, +1% -> 100
                        trend_score = min(100, max(0, 50 + (deviation / 0.01)))  # 1% = 0.01
            except Exception as e:
                logger.warning(f"Trend computation failed for {name}: {e}")
        # 3. Volatility: ATR/price ratio
        vol_score = 50
        try:
            ticker = yf.Ticker(INDEX_SYMBOLS[name])
            hist = ticker.history(period="1mo")
            if len(hist) > 14:
                atr = (hist['High'] - hist['Low']).rolling(14).mean().iloc[-1]
                price = hist['Close'].iloc[-1]
                if price and atr:
                    vol_ratio = atr / price
                    # Map 0% -> 100, 2% -> 0
                    vol_score = min(100, max(0, 100 - (vol_ratio / 0.02) * 100))
        except Exception as e:
            logger.warning(f"Volatility computation failed for {name}: {e}")
        # Combine: 60% momentum, 30% trend, 10% volatility
        combined = 0.60 * mom_score + 0.30 * trend_score + 0.10 * vol_score
        scores.append((combined, w))

    if not scores:
        return 50

    weighted_avg = sum(score * weight for score, weight in scores)
    return int(round(np.clip(weighted_avg, 0, 100)))

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
    if not force_refresh and _cache["data"] is not None:
        cache_age = (now - _cache["timestamp"]).total_seconds() if _cache["timestamp"] else 9999
        if cache_age < _cache["ttl_seconds"]:
            logger.info("Returning cached market sentiment")
            cached_response = _cache["data"].copy()
            cached_response["cached"] = True
            return MarketSentimentResponse(**cached_response)

    indices_data = {}
    for name, symbol in INDEX_SYMBOLS.items():
        data = fetch_index_data_with_retry(symbol, name)
        if data:
            indices_data[name] = data
        else:
            logger.warning(f"Skipping {name} due to fetch error")

    if not indices_data:
        raise HTTPException(status_code=503, detail="Could not fetch any index data")

    score = compute_market_score(indices_data)
    classification = classify_sentiment(score)

    # Derive labels
    trend = "Bullish" if score > 55 else "Bearish" if score < 45 else "Neutral"
    momentum = "Strong" if score > 65 else "Weak" if score < 35 else "Moderate"
    # Breadth: use percentage of indices that are up
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
        "cached": False
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
        "version": "0.3.0",
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