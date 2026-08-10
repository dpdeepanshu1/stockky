"""
Market Sentiment Service
Responsibility: Fetch Indian index data (NIFTY 50, SENSEX, etc.) and compute a
normalized market sentiment score and classification.

v0.2.0 – uses correct Yahoo symbols, adds caching, and improves error handling.
"""
import os
import logging
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any

import yfinance as yf
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("market-sentiment-service")

# --- Configuration ---
# Correct Yahoo Finance symbols for Indian indices
INDEX_SYMBOLS: Dict[str, str] = {
    "NIFTY 50": "^NSEI",
    "SENSEX": "^BSESN",
    "NIFTY Bank": "^NSEBANK",
    "NIFTY IT": "^CNXIT",          # CNX IT
    "NIFTY Auto": "^CNXAUTO",      # CNX Auto
    "NIFTY Pharma": "^CNXPHARMA",  # CNX Pharma
    "NIFTY Metal": "^CNXMETAL",    # CNX Metal
    "NIFTY Energy": "^CNXENERGY",  # CNX Energy
    "NIFTY FMCG": "^CNXFMCG",      # CNX FMCG
}

# --- In-memory cache ---
_cache: Dict[str, Any] = {
    "data": None,
    "timestamp": None,
    "ttl_seconds": 60  # Cache for 60 seconds
}

app = FastAPI(title="Stockky Market Sentiment Service", version="0.2.0")
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
    classification: str  # STRONGLY BULLISH, BULLISH, NEUTRAL, BEARISH, STRONGLY BEARISH
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

def fetch_index_data(symbol: str, name: str) -> Optional[IndexData]:
    """Fetch current and previous day data for a given index symbol."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        # Fallback: if info is empty, try history for latest close
        if not info:
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
                logger.warning(f"Insufficient history for {symbol} ({name})")
                return None

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
    except Exception as e:
        logger.error(f"Error fetching data for {symbol} ({name}): {e}")
        return None

def compute_market_score(indices_data: Dict[str, IndexData]) -> int:
    """
    Compute a normalized market score (0-100) based on:
    - Index trend (price vs 50-day SMA) – if possible.
    - Momentum (change percent)
    - Volatility (ATR proxy)
    - Breadth (simulated)
    """
    scores = []
    
    # 1. Trend: Compare current price to 50-day SMA for each index
    for name, data in indices_data.items():
        if data.current is not None:
            try:
                ticker = yf.Ticker(INDEX_SYMBOLS[name])
                hist = ticker.history(period="3mo")  # get enough data
                if len(hist) >= 50:
                    sma_50 = hist['Close'].rolling(50).mean().iloc[-1]
                    if sma_50 and data.current:
                        # Score: 100 if price > 5% above SMA, 50 if equal, 0 if >5% below
                        deviation = (data.current - sma_50) / sma_50 * 100
                        trend_score = min(100, max(0, 50 + deviation * 10))
                        scores.append(trend_score)
            except Exception as e:
                logger.warning(f"Could not compute trend for {name}: {e}")

    # 2. Momentum: Use change_percent
    for name, data in indices_data.items():
        if data.change_percent is not None:
            # Convert change % to 0-100: -2% -> 0, 0% -> 50, +2% -> 100
            momentum_score = min(100, max(0, (data.change_percent + 2) / 4 * 100))
            scores.append(momentum_score)

    # 3. Volatility: Lower volatility is favourable
    for name, data in indices_data.items():
        try:
            ticker = yf.Ticker(INDEX_SYMBOLS[name])
            hist = ticker.history(period="1mo")
            if len(hist) > 14:
                atr = (hist['High'] - hist['Low']).rolling(14).mean().iloc[-1]
                price = hist['Close'].iloc[-1]
                if price and atr:
                    # Scale: if ATR/price > 0.05 => high volatility => low score
                    vol_ratio = atr / price
                    vol_score = min(100, max(0, 100 - vol_ratio * 2000))
                    scores.append(vol_score)
        except Exception as e:
            logger.warning(f"Could not compute volatility for {name}: {e}")

    # 4. Breadth: Simulated – in production, fetch advance/decline data
    # We'll use the number of indices that are up vs down as proxy
    if indices_data:
        up_count = sum(1 for d in indices_data.values() if d.change and d.change > 0)
        total = len(indices_data)
        breadth_score = (up_count / total) * 100 if total > 0 else 50
        scores.append(breadth_score)

    if not scores:
        return 50  # Neutral if no data

    avg_score = np.mean(scores)
    return int(round(np.clip(avg_score, 0, 100)))

def classify_sentiment(score: int) -> str:
    """Classify market sentiment based on the score."""
    if score >= 80:
        return "STRONGLY BULLISH"
    elif score >= 60:
        return "BULLISH"
    elif score >= 40:
        return "NEUTRAL"
    elif score >= 20:
        return "BEARISH"
    else:
        return "STRONGLY BEARISH"

# --- API Endpoints ---
@app.get("/sentiment", response_model=MarketSentimentResponse)
async def get_market_sentiment(force_refresh: bool = False):
    """Get the current market sentiment. Cached for 60 seconds."""
    now = datetime.now()
    
    # Check cache
    if not force_refresh and _cache["data"] is not None:
        cache_age = (now - _cache["timestamp"]).total_seconds() if _cache["timestamp"] else 9999
        if cache_age < _cache["ttl_seconds"]:
            logger.info("Returning cached market sentiment")
            cached_response = _cache["data"].copy()
            cached_response["cached"] = True
            # Ensure timestamps are updated to now? No, we keep the original timestamp.
            return MarketSentimentResponse(**cached_response)

    # Fetch fresh data
    indices_data = {}
    for name, symbol in INDEX_SYMBOLS.items():
        data = fetch_index_data(symbol, name)
        if data:
            indices_data[name] = data
        else:
            logger.warning(f"Skipping {name} due to fetch error")

    if not indices_data:
        raise HTTPException(status_code=503, detail="Could not fetch any index data")

    score = compute_market_score(indices_data)
    classification = classify_sentiment(score)

    # Determine trend, momentum, breadth, volatility (simplified)
    trend = "Bullish" if score > 60 else "Bearish" if score < 40 else "Neutral"
    momentum = "Strong" if score > 70 else "Weak" if score < 30 else "Moderate"
    breadth = "Positive" if score > 60 else "Negative" if score < 40 else "Mixed"
    volatility = "Normal" if 30 < score < 70 else "High"

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

    # Store in cache
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
        "version": "0.2.0",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/sentiment": "GET – current market sentiment (cached 60s)",
            "/sentiment?force_refresh=true": "GET – force refresh",
        },
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8009))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=True)