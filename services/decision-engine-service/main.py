"""
Decision Engine Service
-------------------------
Combines Technical + Fundamental + News + Prediction scores.
Now includes fundamental metrics in the response, and global error handler.
Gracefully degrades if fundamental or technical analysis fails.
"""
import os
import asyncio
import logging
from enum import Enum

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("decision-engine-service")

TECHNICAL_URL = os.getenv("TECHNICAL_URL", "https://technical-analysis-service-zhnc.onrender.com")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", "https://fundamental-analysis-service.onrender.com")
NEWS_URL = os.getenv("NEWS_URL", "https://news-intelligence-service.onrender.com")
EVENT_URL = os.getenv("EVENT_URL", "https://event-tracker-service-m1lw.onrender.com")
PREDICTION_URL = os.getenv("PREDICTION_URL", "https://prediction-service-wowb.onrender.com")

EVENT_RISK_WINDOW_DAYS = 3

app = FastAPI(title="Stockky Decision Engine", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Global exception handler to always return JSON
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers={"Access-Control-Allow-Origin": "*"}
    )

class Decision(str, Enum):
    BUY_NOW = "BUY NOW"
    PREPARE_TO_BUY = "PREPARE TO BUY"
    HOLD = "HOLD"
    DO_NOT_BUY = "DO NOT BUY"
    SELL = "SELL"

class DecisionRequest(BaseModel):
    already_owned: bool = False

@app.get("/")
def root():
    return {
        "service": "Stockky Decision Engine",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/decide/{symbol}": "GET – get decision for a symbol",
            "/docs": "Swagger UI documentation",
        },
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "decision-engine-service"}

async def _fetch_required(client: httpx.AsyncClient, url: str, label: str) -> dict:
    try:
        resp = await client.get(url, timeout=70)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"{label} unavailable: {e}")

async def _fetch_optional(client: httpx.AsyncClient, url: str, label: str):
    try:
        resp = await client.get(url, timeout=70)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        logger.warning("%s unavailable, continuing without it: %s", label, e)
        return None

def _combined_score(technical_score: int, fundamental_score: int, news_score: int | None, prediction_score: int | None) -> float:
    weights = {"technical": 0.55, "fundamental": 0.45, "news": 0.0, "prediction": 0.0}
    if news_score is not None and prediction_score is not None:
        weights = {"technical": 0.40, "fundamental": 0.30, "news": 0.10, "prediction": 0.20}
    elif news_score is not None:
        weights = {"technical": 0.45, "fundamental": 0.35, "news": 0.20, "prediction": 0.0}
    elif prediction_score is not None:
        weights = {"technical": 0.40, "fundamental": 0.30, "news": 0.0, "prediction": 0.30}
    total = technical_score * weights["technical"] + fundamental_score * weights["fundamental"]
    if news_score is not None:
        total += news_score * weights["news"]
    if prediction_score is not None:
        total += prediction_score * weights["prediction"]
    return round(total, 1)

def _decide(technical_score: int, fundamental_score: int, news_score: int | None, prediction_score: int | None,
            trend_strength: str, volume_surge: bool, dist_to_resistance_pct: float, event_risk: bool, already_owned: bool) -> Decision:
    combined = _combined_score(technical_score, fundamental_score, news_score, prediction_score)
    if already_owned and combined < 35:
        return Decision.SELL
    if already_owned and 35 <= combined < 65:
        return Decision.HOLD
    news_ok = news_score is None or news_score >= 35
    model_ok = prediction_score is None or prediction_score >= 50
    if (technical_score >= 60 and fundamental_score >= 50 
        and trend_strength in ("strong", "moderate") 
        and volume_surge
        and dist_to_resistance_pct is not None and dist_to_resistance_pct > 1
        and news_ok 
        and model_ok):
        if event_risk:
            return Decision.PREPARE_TO_BUY
        return Decision.BUY_NOW
    if fundamental_score >= 45 and 50 <= technical_score < 60:
        return Decision.PREPARE_TO_BUY
    if already_owned and combined >= 65:
        return Decision.HOLD
    return Decision.DO_NOT_BUY

@app.get("/decide/{symbol}")
async def decide(symbol: str, already_owned: bool = False):
    try:
        async with httpx.AsyncClient(timeout=70) as client:
            # Technical: handle missing data gracefully
            try:
                technical_resp = await client.get(f"{TECHNICAL_URL}/analyze/{symbol}", timeout=70)
                technical_resp.raise_for_status()
                technical = technical_resp.json()
                # If technical returned data_insufficient, still use it
                if technical.get("data_insufficient"):
                    logger.info(f"Technical data insufficient for {symbol}, using default values")
            except Exception as e:
                logger.warning(f"Technical analysis failed for {symbol}, using default: {e}")
                technical = {
                    "technical_score": 50,
                    "trend_strength": "unknown",
                    "volume_surge": False,
                    "close": None,
                    "support": None,
                    "resistance": None,
                    "reasons": ["Technical data temporarily unavailable"],
                }

            # Fundamental: catch any exception and use default
            try:
                fundamental_resp = await client.get(f"{FUNDAMENTAL_URL}/analyze/{symbol}", timeout=70)
                fundamental_resp.raise_for_status()
                fundamental = fundamental_resp.json()
            except Exception as e:
                logger.warning(f"Fundamental analysis failed for {symbol}, using default: {e}")
                fundamental = {
                    "fundamental_score": 50,
                    "valuation": "fair",
                    "sector": None,
                    "reasons": ["Fundamental data temporarily unavailable"],
                    "metrics": {},
                    "fallback_used": True
                }

            # Optional services
            news_task = _fetch_optional(client, f"{NEWS_URL}/analyze/{symbol}", "News Intelligence")
            prediction_task = _fetch_optional(client, f"{PREDICTION_URL}/predict/{symbol}", "Prediction Service")
            events_task = _fetch_optional(client, f"{EVENT_URL}/events/{symbol}", "Event Tracker")
            
            news, prediction, events = await asyncio.gather(
                news_task, prediction_task, events_task
            )

        technical_score = technical.get("technical_score", 50)
        fundamental_score = fundamental.get("fundamental_score", 50)
        news_score = news["news_score"] if news else None
        prediction_score = prediction["prediction_score"] if prediction and prediction.get("model_loaded") else None
        fundamental_metrics = fundamental.get("metrics")

        close = technical.get("close")
        support = technical.get("support")
        resistance = technical.get("resistance")
        trend_strength = technical.get("trend_strength", "unknown")
        volume_surge = technical.get("volume_surge", False)

        # Calculate distance to resistance if we have close and resistance
        dist_to_resistance_pct = None
        if close and resistance and resistance > 0:
            dist_to_resistance_pct = round(((resistance - close) / close) * 100, 2)

        event_risk = False
        event_reason = None
        if events and events.get("next_earnings_date"):
            try:
                from datetime import datetime
                earnings_date = datetime.fromisoformat(events["next_earnings_date"][:10])
                days_out = (earnings_date - datetime.utcnow()).days
                if 0 <= days_out <= EVENT_RISK_WINDOW_DAYS:
                    event_risk = True
                    event_reason = f"Earnings due in {days_out} day(s) ({events['next_earnings_date'][:10]}) — elevated volatility risk"
            except (ValueError, TypeError):
                pass

        decision = _decide(technical_score, fundamental_score, news_score, prediction_score,
                           trend_strength, volume_surge, dist_to_resistance_pct,
                           event_risk, already_owned)
        combined_score = _combined_score(technical_score, fundamental_score, news_score, prediction_score)

        # Entry/target/stop only if we have a price
        entry_low, entry_high = None, None
        target = None
        stop_loss = None
        if close:
            support_val = support if support else close * 0.95
            entry_low, entry_high = round(support_val * 1.01, 2), round(close * 1.005, 2)
            target_pct = 0.08
            if prediction_score is not None:
                target_pct = 0.06 + (prediction_score / 100) * 0.05
            target = round(close * (1 + target_pct), 2)
            stop_loss = round(support_val * 0.98, 2)

        confidence = "High" if combined_score >= 75 else "Medium" if combined_score >= 55 else "Low"

        reasons = {
            "technical": technical.get("reasons", ["No technical data"]),
            "fundamental": fundamental.get("reasons", ["No fundamental data"]),
        }
        if news:
            reasons["news"] = news["reasons"]
        if prediction and prediction.get("model_loaded"):
            reasons["prediction"] = [prediction["note"]]
        if event_reason:
            reasons["event"] = [event_reason]

        response = {
            "symbol": symbol.upper(),
            "decision": decision.value,
            "confidence": confidence,
            "combined_score": combined_score,
            "technical_score": technical_score,
            "fundamental_score": fundamental_score,
            "news_score": news_score,
            "prediction_score": prediction_score,
            "event_risk": event_risk,
            "entry_range": {"low": entry_low, "high": entry_high} if entry_low else None,
            "target": target,
            "stop_loss": stop_loss,
            "holding_period": "2-6 weeks" if decision == Decision.BUY_NOW else "N/A",
            "close": close,
            "support": support,
            "resistance": resistance,
            "reasons": reasons,
            "valuation": fundamental.get("valuation", "fair"),
            "sector": fundamental.get("sector"),
        }

        if fundamental_metrics:
            response["fundamental_metrics"] = fundamental_metrics

        return response
    except Exception as e:
        logger.error(f"Decision failed for {symbol}: {e}")
        raise HTTPException(status_code=502, detail=f"Decision failed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8004))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)