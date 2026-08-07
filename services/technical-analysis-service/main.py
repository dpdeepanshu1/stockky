"""
Technical Analysis Service
---------------------------
Single responsibility: turn OHLCV candles (fetched from Market Data Service)
into a technical score (0-100) plus the underlying indicator values and a
human-readable list of reasons. The Decision Engine consumes this score —
this service never makes a buy/sell call itself.
"""
import os
import logging
from typing import List

import httpx
import pandas as pd
import ta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("technical-analysis-service")

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "http://market-data-service:8001")

app = FastAPI(title="Stockky Technical Analysis Service", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def root():
    return {
        "service": "Stockky Technical Analysis Service",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/analyze/{symbol}": "GET – technical score for a symbol",
            "/docs": "Swagger UI documentation",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "technical-analysis-service"}


def _fetch_history(symbol: str, period: str = "6mo") -> pd.DataFrame:
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/history/{symbol}", params={"period": period}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Market data service unreachable: {e}")

    candles = data.get("candles", [])
    if len(candles) < 30:
        raise HTTPException(status_code=422, detail="Not enough history to compute indicators")

    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    df.rename(columns=str.title, inplace=True)  # Open/High/Low/Close/Volume
    return df


def _support_resistance(df: pd.DataFrame, window: int = 20):
    recent = df.tail(window)
    return round(float(recent["Low"].min()), 2), round(float(recent["High"].max()), 2)


@app.get("/analyze/{symbol}")
def analyze(symbol: str):
    df = _fetch_history(symbol)

    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    vol   = df["Volume"]

    # Compute indicators
    df["RSI_14"]   = ta.momentum.rsi(close, window=14)
    df["MACD"]     = ta.trend.macd(close)
    df["MACDs"]    = ta.trend.macd_signal(close)
    df["EMA_20"]   = ta.trend.ema_indicator(close, window=20)
    df["EMA_50"]   = ta.trend.ema_indicator(close, window=50)
    df["EMA_200"]  = ta.trend.ema_indicator(close, window=200)
    df["ADX_14"]   = ta.trend.adx(high, low, close, window=14)
    df["ATR_14"]   = ta.volatility.average_true_range(high, low, close, window=14)
    df["BB_upper"] = ta.volatility.bollinger_hband(close, window=20)
    df["BB_lower"] = ta.volatility.bollinger_lband(close, window=20)
    df["PSAR_up"]  = ta.trend.psar_up_indicator(high, low, close)   # 1 = bullish, 0 = bearish
    df["VWAP"]     = ta.volume.volume_weighted_average_price(high, low, close, vol)

    latest   = df.iloc[-1]
    prev     = df.iloc[-2]
    close_val = float(latest["Close"])
    support, resistance = _support_resistance(df)

    reasons: List[str] = []
    score = 50  # neutral baseline

    # RSI
    rsi = float(latest["RSI_14"] or 50)
    if rsi < 30:
        score += 12
        reasons.append(f"RSI at {rsi:.1f} — oversold, potential reversal zone")
    elif rsi > 70:
        score -= 12
        reasons.append(f"RSI at {rsi:.1f} — overbought, momentum may be exhausted")
    else:
        reasons.append(f"RSI at {rsi:.1f} — neutral momentum")

    # MACD crossover
    macd_val     = float(latest["MACD"]  or 0)
    macd_signal  = float(latest["MACDs"] or 0)
    prev_macd    = float(prev["MACD"]    or 0)
    prev_signal  = float(prev["MACDs"]   or 0)
    bullish_cross = prev_macd < prev_signal and macd_val > macd_signal
    bearish_cross = prev_macd > prev_signal and macd_val < macd_signal
    if bullish_cross:
        score += 15
        reasons.append("MACD just crossed above signal line — bullish crossover")
    elif bearish_cross:
        score -= 15
        reasons.append("MACD just crossed below signal line — bearish crossover")
    elif macd_val > macd_signal:
        score += 5
        reasons.append("MACD above signal line — bullish bias intact")
    else:
        score -= 5
        reasons.append("MACD below signal line — bearish bias intact")

    # EMA trend stack
    ema20  = float(latest["EMA_20"]  or close_val)
    ema50  = float(latest["EMA_50"]  or close_val)
    ema200 = float(latest["EMA_200"] or close_val)
    if close_val > ema20 > ema50 > ema200:
        score += 15
        reasons.append("Price above EMA20/50/200 in bullish stack — strong uptrend")
    elif close_val < ema20 < ema50 < ema200:
        score -= 15
        reasons.append("Price below EMA20/50/200 in bearish stack — strong downtrend")
    elif close_val > ema200:
        score += 5
        reasons.append("Price above 200 EMA — long-term trend still bullish")
    else:
        score -= 5
        reasons.append("Price below 200 EMA — long-term trend bearish")

    # PSAR direction (replaces Supertrend — same concept: trend direction signal)
    psar_bullish = float(latest["PSAR_up"] or 0) == 1.0
    if psar_bullish:
        score += 10
        reasons.append("Parabolic SAR in bullish mode")
    else:
        score -= 10
        reasons.append("Parabolic SAR in bearish mode")

    # ADX trend strength
    adx_val = float(latest["ADX_14"] or 0)
    trend_strength = "weak"
    if adx_val > 25:
        trend_strength = "strong"
        reasons.append(f"ADX at {adx_val:.1f} — strong trend, higher conviction signal")
    else:
        reasons.append(f"ADX at {adx_val:.1f} — weak/no trend, range-bound caution")

    # Proximity to support/resistance
    dist_to_resistance_pct = round(((resistance - close_val) / close_val) * 100, 2)
    dist_to_support_pct    = round(((close_val - support)   / close_val) * 100, 2)
    if dist_to_resistance_pct < 2:
        score -= 8
        reasons.append(f"Price only {dist_to_resistance_pct}% below resistance ({resistance}) — breakout needed")
    if dist_to_support_pct < 2:
        score += 8
        reasons.append(f"Price only {dist_to_support_pct}% above support ({support}) — favorable risk/reward")

    # Volume confirmation
    avg_vol_20  = float(df["Volume"].tail(20).mean())
    latest_vol  = float(latest["Volume"])
    volume_surge = latest_vol > avg_vol_20 * 1.5
    if volume_surge:
        score += 8
        reasons.append("Volume surging vs 20-day average — institutional interest likely")

    score = max(0, min(100, round(score)))

    return {
        "symbol":           symbol.upper(),
        "close":            round(close_val, 2),
        "technical_score":  score,
        "trend_strength":   trend_strength,
        "support":          support,
        "resistance":       resistance,
        "rsi":              round(rsi, 1),
        "volume_surge":     volume_surge,
        "reasons":          reasons,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8002))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)