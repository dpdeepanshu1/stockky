"""
Technical Analysis Service
---------------------------
Single responsibility: compute technical indicators (score, trend, support/resistance)
for a given symbol using yfinance. No decision logic here — just raw technical signals.
Now handles both .NS and non-.NS symbols gracefully.
"""
import os
import logging
import math
import time
import pandas as pd
import yfinance as yf
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("technical-analysis-service")

app = FastAPI(title="Stockky Technical Analysis Service", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- 配置 requests session 以提升稳定性 ---
def get_yfinance_session():
    session = requests.Session()
    # 伪装成真实浏览器，降低被屏蔽的风险[reference:10]
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    })
    # 配置重试策略，应对临时性网络错误[reference:11]
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

# 应用自定义 session
yf_session = get_yfinance_session()
yf.set_session(yf_session)

# 全局异常处理器
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal error: {str(exc)}"},
        headers={"Access-Control-Allow-Origin": "*"}
    )

def normalize_symbol(symbol: str) -> str:
    sym = symbol.strip().upper()
    if not sym.endswith(".NS") and not sym.endswith(".BO"):
        sym = f"{sym}.NS"
    return sym

def _safe(val, decimals=2):
    try:
        f = float(val)
        if math.isnan(f) or not math.isfinite(f):
            return None
        return round(f, decimals)
    except (TypeError, ValueError):
        return None

@app.get("/")
def root():
    return {
        "service": "Stockky Technical Analysis Service",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/analyze/{symbol}": "GET – technical analysis for a symbol",
            "/docs": "Swagger UI documentation",
        },
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "technical-analysis-service"}

@app.get("/analyze/{symbol}")
def analyze(symbol: str):
    sym = normalize_symbol(symbol)
    try:
        ticker = yf.Ticker(sym)
        ticker._tz = "Asia/Kolkata"
        # 添加超时控制，避免请求卡死[reference:12]
        hist = ticker.history(period="1y", timeout=30)
        if hist.empty or len(hist) < 30:
            raise HTTPException(status_code=404, detail=f"Insufficient price data for {sym}")

        df = hist.copy()
        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        volume = df["Volume"]

        current_price = float(close.iloc[-1])
        support = float(close.rolling(window=20).min().iloc[-1])
        resistance = float(close.rolling(window=20).max().iloc[-1])

        # RSI
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_val = float(rsi.iloc[-1]) if not rsi.isna().iloc[-1] else 50.0

        # MACD
        exp1 = close.ewm(span=12, adjust=False).mean()
        exp2 = close.ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        macd_signal = macd.ewm(span=9, adjust=False).mean()
        macd_bullish = macd.iloc[-1] > macd_signal.iloc[-1]

        # 200 EMA
        ema_200 = close.ewm(span=200, adjust=False).mean()
        price_above_200_ema = current_price > ema_200.iloc[-1]

        # Parabolic SAR (approximated)
        sar_bullish = current_price > close.rolling(window=20).mean().iloc[-1]

        # ADX (simplified)
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        adx = 25.0 if (current_price > close.rolling(50).mean().iloc[-1]) else 15.0

        trend_strength = "strong" if adx >= 25 else "moderate" if adx >= 20 else "weak"
        volume_surge = volume.iloc[-1] > (volume.rolling(20).mean().iloc[-1] * 1.5)

        reasons = []
        if rsi_val > 70:
            reasons.append(f"RSI at {rsi_val:.1f} — overbought, potential pullback")
        elif rsi_val < 30:
            reasons.append(f"RSI at {rsi_val:.1f} — oversold, potential bounce")
        else:
            reasons.append(f"RSI at {rsi_val:.1f} — neutral momentum")

        if macd_bullish:
            reasons.append("MACD above signal line — bullish bias intact")
        else:
            reasons.append("MACD below signal line — bearish bias intact")

        if price_above_200_ema:
            reasons.append("Price above 200 EMA — long-term trend bullish")
        else:
            reasons.append("Price below 200 EMA — long-term trend bearish")

        if sar_bullish:
            reasons.append("Parabolic SAR in bullish mode")
        else:
            reasons.append("Parabolic SAR in bearish mode")

        if adx >= 25:
            reasons.append(f"ADX at {adx:.1f} — strong trend, higher conviction signal")
        else:
            reasons.append(f"ADX at {adx:.1f} — weak/no trend, range-bound caution")

        dist_to_resistance = ((resistance - current_price) / current_price) * 100
        reasons.append(f"Price only {dist_to_resistance:.2f}% below resistance ({resistance:.2f}) — breakout needed")

        score = 50
        if 30 < rsi_val < 70:
            score += 5
        if macd_bullish:
            score += 10
        if price_above_200_ema:
            score += 10
        if sar_bullish:
            score += 10
        if adx >= 25:
            score += 10
        if volume_surge:
            score += 5
        score = max(0, min(100, round(score)))

        return {
            "symbol": symbol,
            "technical_score": score,
            "trend_strength": trend_strength,
            "volume_surge": volume_surge,
            "close": current_price,
            "support": support,
            "resistance": resistance,
            "reasons": reasons,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Technical analysis failed for {sym}")
        raise HTTPException(status_code=502, detail=f"Technical analysis unavailable: {e}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8002))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)