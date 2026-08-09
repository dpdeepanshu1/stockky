"""
Technical Analysis Service
---------------------------
Single responsibility: compute technical indicators (score, trend,
support/resistance) for a given NSE symbol.

Data source: fetches OHLCV candles from the Market Data Service (which
handles yfinance, caching, and the _tz bypass). Never calls yfinance
directly — avoids rate limits and duplicate network calls.

All indicators computed with pure pandas — no external TA library needed.
"""
import os
import logging
import math

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("technical-analysis-service")

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://stockky-market-data.onrender.com")

app = FastAPI(title="Stockky Technical Analysis Service", version="0.2.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Helpers ────────────────────────────────────────────────────────────────────
def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace(".NS", "").replace(".BO", "")


def _safe(val, decimals=2):
    try:
        f = float(val)
        return round(f, decimals) if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _fetch_quote_price(symbol: str) -> float | None:
    """Fetch current price from market-data service quote endpoint."""
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("price")
    except Exception:
        pass
    return None


def _fetch_history(symbol: str):
    """Fetch OHLCV candles from market-data-service. Returns DataFrame or None if insufficient data."""
    try:
        resp = httpx.get(
            f"{MARKET_DATA_URL}/history/{symbol}",
            params={"period": "1y"},
            timeout=25,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Market data service unreachable: {e}")

    candles = data.get("candles", [])
    if len(candles) < 5:
        return None

    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)

    # Rename columns to standard Title case
    rename = {}
    for col in df.columns:
        rename[col] = col.capitalize()
    df.rename(columns=rename, inplace=True)

    # Ensure numeric
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(subset=["Close"], inplace=True)
    return df


# ── Indicator calculations ─────────────────────────────────────────────────────
def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def _macd(close: pd.Series):
    exp12 = _ema(close, 12)
    exp26 = _ema(close, 26)
    macd_line = exp12 - exp26
    signal = _ema(macd_line, 9)
    return macd_line, signal


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)

    dm_pos = high.diff()
    dm_neg = -low.diff()
    dm_pos = dm_pos.where((dm_pos > dm_neg) & (dm_pos > 0), 0.0)
    dm_neg = dm_neg.where((dm_neg > dm_pos) & (dm_neg > 0), 0.0)

    atr   = tr.rolling(period).mean()
    di_pos = 100 * dm_pos.rolling(period).mean() / atr.replace(0, float("nan"))
    di_neg = 100 * dm_neg.rolling(period).mean() / atr.replace(0, float("nan"))
    dx = 100 * (di_pos - di_neg).abs() / (di_pos + di_neg).replace(0, float("nan"))
    return dx.rolling(period).mean()


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _bollinger(close: pd.Series, period: int = 20):
    mid  = close.rolling(period).mean()
    std  = close.rolling(period).std()
    return mid + 2 * std, mid - 2 * std


def _support_resistance(df: pd.DataFrame, window: int = 20):
    recent = df.tail(window)
    return float(recent["Low"].min()), float(recent["High"].max())


# ── Main route ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "technical-analysis-service"}


@app.get("/")
def root():
    return {"service": "technical-analysis-service", "version": "0.2.1", "status": "ok",
            "endpoints": ["/health", "/analyze/{symbol}"]}


@app.get("/analyze/{symbol}")
def analyze(symbol: str):
    sym = normalize_symbol(symbol)
    df = _fetch_history(sym)

    # ── Handle insufficient data (newly listed stocks) ──
    if df is None or len(df) < 5:
        # Try to get current price from quote endpoint
        price = _fetch_quote_price(sym)
        if price:
            # Even with only price, we can still return something
            return {
                "symbol": sym,
                "technical_score": 50,
                "trend_strength": "unknown",
                "volume_surge": False,
                "close": price,
                "support": None,
                "resistance": None,
                "data_insufficient": True,
                "reasons": [
                    f"Insufficient price history for {sym} (newly listed stock). "
                    f"Current price: ₹{price:.2f}. Please check back in 2-3 days for full analysis."
                ],
            }
        else:
            # No price even from quote
            return {
                "symbol": sym,
                "technical_score": 50,
                "trend_strength": "unknown",
                "volume_surge": False,
                "close": None,
                "support": None,
                "resistance": None,
                "data_insufficient": True,
                "reasons": [
                    f"Insufficient price data for {sym} (newly listed stock). "
                    "Please check back in 2-3 days after Yahoo Finance updates its database."
                ],
            }
    # ──────────────────────────────────────────────────────

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    # ── Compute indicators ─────────────────────────────────────────────────────
    # Only compute if we have enough data; else use defaults
    data_length = len(df)
    rsi_series = _rsi(close) if data_length >= 14 else pd.Series([50]*data_length, index=df.index)
    macd_line, macd_sig = _macd(close) if data_length >= 26 else (pd.Series([0]*data_length, index=df.index), pd.Series([0]*data_length, index=df.index))
    ema20 = _ema(close, min(20, data_length)) if data_length >= 5 else close
    ema50 = _ema(close, min(50, data_length)) if data_length >= 10 else close
    ema200 = _ema(close, min(200, data_length)) if data_length >= 30 else close
    adx_series = _adx(high, low, close) if data_length >= 20 else pd.Series([15]*data_length, index=df.index)
    atr_series = _atr(high, low, close) if data_length >= 14 else pd.Series([0]*data_length, index=df.index)
    bb_upper, bb_lower = _bollinger(close, min(20, data_length)) if data_length >= 5 else (close, close)

    latest = df.iloc[-1]
    close_val = float(latest["Close"])
    support, resistance = _support_resistance(df, min(20, len(df)))

    rsi_val    = _safe(rsi_series.iloc[-1])   or 50.0
    macd_val   = _safe(macd_line.iloc[-1])    or 0.0
    macd_s_val = _safe(macd_sig.iloc[-1])     or 0.0
    prev_macd  = _safe(macd_line.iloc[-2])    or 0.0 if len(macd_line) > 1 else 0.0
    prev_sig   = _safe(macd_sig.iloc[-2])     or 0.0 if len(macd_sig) > 1 else 0.0
    ema20_val  = _safe(ema20.iloc[-1])        or close_val
    ema50_val  = _safe(ema50.iloc[-1])        or close_val
    ema200_val = _safe(ema200.iloc[-1])       or close_val
    adx_val    = _safe(adx_series.iloc[-1])   or 0.0
    atr_val    = _safe(atr_series.iloc[-1])   or 0.0
    bb_up      = _safe(bb_upper.iloc[-1])     or close_val
    bb_lo      = _safe(bb_lower.iloc[-1])     or close_val
    vol_now    = float(latest["Volume"])
    vol_avg20  = float(volume.tail(min(20, len(volume))).mean()) if len(volume) >= 5 else vol_now

    # ── Score (baseline 50) ────────────────────────────────────────────────────
    score   = 50
    reasons = []

    # RSI
    if rsi_val < 30:
        score += 12
        reasons.append(f"RSI at {rsi_val:.1f} — oversold, potential reversal zone")
    elif rsi_val > 70:
        score -= 12
        reasons.append(f"RSI at {rsi_val:.1f} — overbought, momentum may be exhausted")
    else:
        reasons.append(f"RSI at {rsi_val:.1f} — neutral momentum")

    # MACD (only if we had enough data)
    if data_length >= 26:
        bullish_cross = prev_macd < prev_sig and macd_val > macd_s_val
        bearish_cross = prev_macd > prev_sig and macd_val < macd_s_val
        if bullish_cross:
            score += 15
            reasons.append("MACD just crossed above signal — bullish crossover")
        elif bearish_cross:
            score -= 15
            reasons.append("MACD just crossed below signal — bearish crossover")
        elif macd_val > macd_s_val:
            score += 5
            reasons.append("MACD above signal line — bullish bias intact")
        else:
            score -= 5
            reasons.append("MACD below signal line — bearish bias intact")
    else:
        reasons.append("MACD: insufficient data for full calculation")

    # EMA trend stack
    if data_length >= 30:
        if close_val > ema20_val > ema50_val > ema200_val:
            score += 15
            reasons.append("Price above EMA20/50/200 in bullish stack — strong uptrend")
        elif close_val < ema20_val < ema50_val < ema200_val:
            score -= 15
            reasons.append("Price below EMA20/50/200 in bearish stack — strong downtrend")
        elif close_val > ema200_val:
            score += 5
            reasons.append("Price above 200 EMA — long-term trend bullish")
        else:
            score -= 5
            reasons.append("Price below 200 EMA — long-term trend bearish")
    else:
        reasons.append("EMA trend: insufficient data for full trend analysis")

    # Parabolic SAR proxy (price vs 20-period SMA)
    if data_length >= 20:
        sma20 = float(close.tail(20).mean())
        sar_bullish = close_val > sma20
        if sar_bullish:
            score += 8
            reasons.append("Price above 20-day average — short-term momentum positive")
        else:
            score -= 8
            reasons.append("Price below 20-day average — short-term momentum negative")
    else:
        reasons.append("Short-term momentum: insufficient data")

    # ADX trend strength
    trend_strength = "strong" if adx_val >= 25 else "moderate" if adx_val >= 20 else "weak"
    if data_length >= 20:
        if adx_val >= 25:
            reasons.append(f"ADX at {adx_val:.1f} — strong trend, higher conviction signal")
        else:
            reasons.append(f"ADX at {adx_val:.1f} — weak/no trend, range-bound caution")
    else:
        reasons.append("ADX: insufficient data for trend strength")

    # Bollinger Band position
    if data_length >= 20:
        bb_range = bb_up - bb_lo if bb_up != bb_lo else 1
        bb_pct   = (close_val - bb_lo) / bb_range * 100
        if bb_pct < 20:
            score += 8
            reasons.append(f"Price near lower Bollinger Band ({bb_pct:.0f}%) — oversold zone")
        elif bb_pct > 80:
            score -= 8
            reasons.append(f"Price near upper Bollinger Band ({bb_pct:.0f}%) — overbought zone")
    else:
        reasons.append("Bollinger Bands: insufficient data")

    # Proximity to support/resistance
    dist_res = round(((resistance - close_val) / close_val) * 100, 2) if resistance and close_val else 999
    dist_sup = round(((close_val - support)   / close_val) * 100, 2) if support and close_val else 999
    if dist_res < 2:
        score -= 8
        reasons.append(f"Price only {dist_res}% below resistance ({resistance:.0f}) — breakout needed")
    if dist_sup < 2:
        score += 8
        reasons.append(f"Price only {dist_sup}% above support ({support:.0f}) — favorable risk/reward")

    # Volume
    volume_surge = vol_avg20 > 0 and vol_now > vol_avg20 * 1.5
    if volume_surge:
        score += 8
        reasons.append("Volume surging vs 20-day average — institutional interest likely")

    score = max(0, min(100, round(score)))

    return {
        "symbol":          sym,
        "close":           round(close_val, 2),
        "technical_score": score,
        "trend_strength":  trend_strength,
        "support":         round(support, 2) if support else None,
        "resistance":      round(resistance, 2) if resistance else None,
        "rsi":             round(rsi_val, 1),
        "adx":             round(adx_val, 1),
        "atr":             round(atr_val, 2),
        "ema20":           round(ema20_val, 2),
        "ema50":           round(ema50_val, 2),
        "ema200":          round(ema200_val, 2),
        "bb_upper":        round(bb_up, 2),
        "bb_lower":        round(bb_lo, 2),
        "volume_surge":    bool(volume_surge),
        "data_insufficient": data_length < 30,
        "reasons":         reasons,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8002))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)