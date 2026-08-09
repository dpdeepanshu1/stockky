"""
Market Sentiment Service
Responsibility: Fetch Indian index data (NIFTY 50, SENSEX, etc.) and compute a
normalized market sentiment score and classification.
"""
import os
import logging
from datetime import datetime
from typing import List, Dict, Optional

import yfinance as yf
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("market-sentiment-service")

# --- Configuration ---
# Yahoo Finance symbols for Indian indices
INDEX_SYMBOLS = {
    "NIFTY 50": "^NSEI",
    "SENSEX": "^BSESN",
    "NIFTY Bank": "^NSEBANK",
    "NIFTY IT": "NIFTY_IT.NS",  # Placeholder, may need adjustment
    "NIFTY Auto": "NIFTY_AUTO.NS",
    "NIFTY Pharma": "NIFTY_PHARMA.NS",
    "NIFTY Metal": "NIFTY_METAL.NS",
    "NIFTY Energy": "NIFTY_ENERGY.NS",
    "NIFTY FMCG": "NIFTY_FMCG.NS",
}

app = FastAPI(title="Stockky Market Sentiment Service", version="0.1.0")
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
        # Get current quote
        info = ticker.info
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
    - Index trend (price vs moving averages)
    - Momentum (RSI)
    - Breadth (advance/decline - simulated for now)
    - Volatility (ATR)
    """
    scores = []
    
    # 1. Trend: Compare current price to 50-day SMA
    for name, data in indices_data.items():
        if data.current and data.previous_close:
            try:
                ticker = yf.Ticker(INDEX_SYMBOLS[name])
                hist = ticker.history(period="2mo")
                if len(hist) >= 50:
                    sma_50 = hist['Close'].rolling(50).mean().iloc[-1]
                    if sma_50:
                        trend_score = min(100, max(0, ((data.current - sma_50) / sma_50) * 100 + 50))
                        scores.append(trend_score)
            except Exception as e:
                logger.warning(f"Could not compute trend for {name}: {e}")

    # 2. Momentum: Use RSI (simplified)
    # For a real implementation, fetch RSI from technical service or compute here.
    # We'll use a placeholder: if change_percent > 0.5, bullish momentum.
    for name, data in indices_data.items():
        if data.change_percent is not None:
            # Convert change % to a 0-100 score: -2% -> 0, 0% -> 50, +2% -> 100
            momentum_score = min(100, max(0, (data.change_percent + 2) / 4 * 100))
            scores.append(momentum_score)

    # 3. Volatility: Lower volatility is generally better for trend-following
    # We'll use ATR as a proxy (higher ATR = higher volatility = lower score)
    for name, data in indices_data.items():
        try:
            ticker = yf.Ticker(INDEX_SYMBOLS[name])
            hist = ticker.history(period="1mo")
            if len(hist) > 14:
                atr = (hist['High'] - hist['Low']).rolling(14).mean().iloc[-1]
                price = hist['Close'].iloc[-1]
                if price and atr:
                    vol_score = min(100, max(0, 100 - (atr / price) * 200))
                    scores.append(vol_score)
        except Exception as e:
            logger.warning(f"Could not compute volatility for {name}: {e}")

    # 4. Breadth: Placeholder - in a real implementation, fetch advance/decline data
    # For now, we'll add a neutral score
    scores.append(50)

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
async def get_market_sentiment():
    """Get the current market sentiment."""
    indices_data = {}
    for name, symbol in INDEX_SYMBOLS.items():
        data = fetch_index_data(symbol, name)
        if data:
            indices_data[name] = data

    if not indices_data:
        raise HTTPException(status_code=503, detail="Could not fetch index data")

    score = compute_market_score(indices_data)
    classification = classify_sentiment(score)

    # Determine trend, momentum, breadth, volatility (simplified)
    trend = "Bullish" if score > 60 else "Bearish" if score < 40 else "Neutral"
    momentum = "Strong" if score > 70 else "Weak" if score < 30 else "Moderate"
    breadth = "Positive" if score > 60 else "Negative" if score < 40 else "Mixed"
    volatility = "Normal" if 30 < score < 70 else "High"

    return MarketSentimentResponse(
        timestamp=datetime.now(),
        indices=indices_data,
        market_score=score,
        classification=classification,
        trend=trend,
        momentum=momentum,
        breadth=breadth,
        volatility=volatility
    )

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "market-sentiment-service"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8009)