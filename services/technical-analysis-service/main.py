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
import pandas_ta as ta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("technical-analysis-service")

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "http://market-data-service:8001")

app = FastAPI(title="Stockky Technical Analysis Service", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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
    df.rename(columns=str.title, inplace=True)  # Open/High/Low/Close/Volume for pandas_ta
    return df


def _support_resistance(df: pd.DataFrame, window: int = 20):
    recent = df.tail(window)
    return round(float(recent["Low"].min()), 2), round(float(recent["High"].max()), 2)


@app.get("/analyze/{symbol}")
def analyze(symbol: str):
    df = _fetch_history(symbol)

    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.ema(length=20, append=True)
    df.ta.ema(length=50, append=True)
    df.ta.ema(length=200, append=True)
    df.ta.adx(length=14, append=True)
    df.ta.atr(length=14, append=True)
    df.ta.bbands(length=20, append=True)
    df.ta.supertrend(length=10, multiplier=3, append=True)
    df.ta.vwap(append=True)

    latest = df.iloc[-1]
    close = float(latest["Close"])
    support, resistance = _support_resistance(df)

    reasons: List[str] = []
    score = 50  # neutral baseline, adjusted by each signal below

    # RSI
    rsi = float(latest.get("RSI_14", 50) or 50)
    if rsi < 30:
        score += 12
        reasons.append(f"RSI at {rsi:.1f} — oversold, potential reversal zone")
    elif rsi > 70:
        score -= 12
        reasons.append(f"RSI at {rsi:.1f} — overbought, momentum may be exhausted")
    else:
        reasons.append(f"RSI at {rsi:.1f} — neutral momentum")

    # MACD crossover
    macd_col = next((c for c in df.columns if c.startswith("MACD_") and not c.startswith("MACDh") and not c.startswith("MACDs")), None)
    macds_col = next((c for c in df.columns if c.startswith("MACDs_")), None)
    if macd_col and macds_col:
        macd_val = float(latest[macd_col])
        macd_signal = float(latest[macds_col])
        prev_macd = float(df.iloc[-2][macd_col])
        prev_signal = float(df.iloc[-2][macds_col])
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
    ema20 = float(latest.get("EMA_20", close))
    ema50 = float(latest.get("EMA_50", close))
    ema200 = float(latest.get("EMA_200", close))
    if close > ema20 > ema50 > ema200:
        score += 15
        reasons.append("Price above EMA20/50/200 in bullish stack — strong uptrend")
    elif close < ema20 < ema50 < ema200:
        score -= 15
        reasons.append("Price below EMA20/50/200 in bearish stack — strong downtrend")
    elif close > ema200:
        score += 5
        reasons.append("Price above 200 EMA — long-term trend still bullish")
    else:
        score -= 5
        reasons.append("Price below 200 EMA — long-term trend bearish")

    # Supertrend direction
    st_dir_col = next((c for c in df.columns if c.startswith("SUPERTd_")), None)
    if st_dir_col:
        st_dir = float(latest[st_dir_col])
        if st_dir == 1:
            score += 10
            reasons.append("Supertrend in bullish mode")
        else:
            score -= 10
            reasons.append("Supertrend in bearish mode")

    # ADX trend strength
    adx_col = next((c for c in df.columns if c.startswith("ADX_")), None)
    trend_strength = "weak"
    if adx_col:
        adx_val = float(latest[adx_col])
        if adx_val > 25:
            trend_strength = "strong"
            reasons.append(f"ADX at {adx_val:.1f} — strong trend, higher conviction signal")
        else:
            reasons.append(f"ADX at {adx_val:.1f} — weak/no trend, range-bound caution")

    # Proximity to support/resistance
    dist_to_resistance_pct = round(((resistance - close) / close) * 100, 2)
    dist_to_support_pct = round(((close - support) / close) * 100, 2)
    if dist_to_resistance_pct < 2:
        score -= 8
        reasons.append(f"Price only {dist_to_resistance_pct}% below resistance ({resistance}) — breakout needed")
    if dist_to_support_pct < 2:
        score += 8
        reasons.append(f"Price only {dist_to_support_pct}% above support ({support}) — favorable risk/reward")

    # Volume confirmation
    avg_vol_20 = float(df["Volume"].tail(20).mean())
    latest_vol = float(latest["Volume"])
    volume_surge = latest_vol > avg_vol_20 * 1.5
    if volume_surge:
        score += 8
        reasons.append("Volume surging vs 20-day average — institutional interest likely")

    score = max(0, min(100, round(score)))

    return {
        "symbol": symbol.upper(),
        "close": round(close, 2),
        "technical_score": score,
        "trend_strength": trend_strength,
        "support": support,
        "resistance": resistance,
        "rsi": round(rsi, 1),
        "volume_surge": volume_surge,
        "reasons": reasons,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True)
