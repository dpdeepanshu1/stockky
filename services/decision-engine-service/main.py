"""
Decision Engine Service v0.3.2
--------------------------------
Combines Technical + Fundamental + News + Event + Prediction scores.

v0.3.2 changes:
  - Fixed int conversion errors when news_score or prediction_score is None.
  - Wrapped entire /decide endpoint in try/except to always return 200 with fallback values.
  - Improved error logging for debugging.
"""
import os
import asyncio
import logging
from datetime import datetime
from enum import Enum

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("decision-engine-service")

TECHNICAL_URL   = os.getenv("TECHNICAL_URL",   "https://technical-analysis-service-zhnc.onrender.com")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", "https://fundamental-analysis-service.onrender.com")
NEWS_URL        = os.getenv("NEWS_URL",         "https://news-intelligence-service.onrender.com")
EVENT_URL       = os.getenv("EVENT_URL",        "https://event-tracker-service-m1lw.onrender.com")
PREDICTION_URL  = os.getenv("PREDICTION_URL",   "https://prediction-service-wowb.onrender.com")

EARNINGS_RISK_DAYS   = 3
EARNINGS_BOOST_DAYS  = 7

app = FastAPI(title="Stockky Decision Engine", version="0.3.2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers={"Access-Control-Allow-Origin": "*"},
    )


class Decision(str, Enum):
    BUY_NOW        = "BUY NOW"
    PREPARE_TO_BUY = "PREPARE TO BUY"
    HOLD           = "HOLD"
    WAIT           = "WAIT"        # NEW: For newly listed/insufficient data but positive news
    DO_NOT_BUY     = "DO NOT BUY"
    SELL           = "SELL"


@app.get("/")
def root():
    return {"service": "Stockky Decision Engine", "version": "0.3.2", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "decision-engine-service"}


# ── Fetch helpers ──────────────────────────────────────────────────────────────
async def _fetch_optional(client: httpx.AsyncClient, url: str, label: str):
    try:
        resp = await client.get(url, timeout=70)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        logger.warning("%s unavailable: %s", label, e)
        return None


# ── Event signal extraction ────────────────────────────────────────────────────
def _extract_event_signals(events: dict | None) -> dict:
    if not events or not isinstance(events, dict):
        return {"event_score_delta": 0, "event_risk": False,
                "event_reasons": [], "earnings_days_out": None}

    delta = 0
    reasons = []
    event_risk = False
    earnings_days_out = None
    now = datetime.utcnow()

    # Earnings proximity
    next_earnings = events.get("next_earnings_date")
    if next_earnings:
        try:
            earnings_dt = datetime.fromisoformat(str(next_earnings)[:10])
            days_out = (earnings_dt - now).days
            earnings_days_out = days_out

            if 0 <= days_out <= EARNINGS_RISK_DAYS:
                event_risk = True
                reasons.append(f"⚠ Earnings in {days_out}d ({next_earnings[:10]}) — hold off, high volatility risk")
                delta -= 5
            elif 0 < days_out <= EARNINGS_BOOST_DAYS:
                delta += 8
                reasons.append(f"📅 Earnings in {days_out}d — pre-results momentum window")
            elif days_out < 0 and days_out >= -30:
                reasons.append(f"📋 Recent earnings ({abs(days_out)}d ago)")
        except (ValueError, TypeError):
            pass

    # Earnings surprise
    earnings_surprise = events.get("earnings_surprise")
    if earnings_surprise and isinstance(earnings_surprise, dict):
        surprise_pct = earnings_surprise.get("surprise_pct")
        if surprise_pct is not None:
            if surprise_pct > 5:
                delta += 6
                reasons.append(f"📈 Earnings surprise: +{surprise_pct:.1f}% beat")
            elif surprise_pct < -5:
                delta -= 6
                reasons.append(f"📉 Earnings surprise: {surprise_pct:.1f}% miss")

    # Analyst upgrades/downgrades
    analyst_actions = events.get("recent_analyst_actions") or []
    for action in analyst_actions[:2]:
        act = str(action.get("action", "")).lower()
        grade = str(action.get("to_grade", "")).lower()
        firm = action.get("firm", "")
        if act in ("upgrade", "upgraded") or grade in ("buy", "strong buy", "outperform", "overweight"):
            delta += 6
            reasons.append(f"📈 Analyst upgrade: {firm} → {grade}")
            break
        elif act in ("downgrade", "downgraded") or grade in ("sell", "underperform", "underweight"):
            delta -= 6
            reasons.append(f"📉 Analyst downgrade: {firm} → {grade}")
            break

    # Insider transactions
    insider_txns = events.get("recent_insider_transactions") or []
    for txn in insider_txns[:2]:
        txn_type = str(txn.get("transaction", "")).lower()
        shares = txn.get("shares") or 0
        if "buy" in txn_type or "purchase" in txn_type:
            if shares and shares > 1000:
                delta += 5
                reasons.append(f"🟢 Insider buying: {txn.get('insider', 'insider')} bought {shares:,} shares")
                break
        elif "sell" in txn_type and "sale" in txn_type:
            delta -= 3
            reasons.append(f"🔴 Insider selling: {txn.get('insider', 'insider')} sold shares")
            break

    # Bulk/Block deals
    bulk_deals = events.get("bulk_deals") or []
    if bulk_deals:
        delta += 4
        reasons.append(f"📊 Bulk/Block deal detected")

    # FII/DII net flow
    fii_flow = events.get("fii_dii_net_flow")
    if fii_flow and isinstance(fii_flow, dict):
        net = fii_flow.get("net")
        if net is not None:
            if net > 0:
                delta += 3
                reasons.append(f"📈 FII/DII net inflow positive")
            elif net < 0:
                delta -= 3
                reasons.append(f"📉 FII/DII net outflow negative")

    return {
        "event_score_delta": max(-15, min(15, delta)),
        "event_risk": event_risk,
        "event_reasons": reasons,
        "earnings_days_out": earnings_days_out,
    }


# ── Combined score ─────────────────────────────────────────────────────────────
def _combined_score(
    technical_score: int,
    fundamental_score: int,
    news_score: int | None,
    prediction_score: int | None,
    event_delta: int = 0,
) -> float:
    if news_score is not None and prediction_score is not None:
        weights = {"t": 0.38, "f": 0.28, "n": 0.14, "p": 0.20}
    elif news_score is not None:
        weights = {"t": 0.42, "f": 0.33, "n": 0.25, "p": 0.0}
    elif prediction_score is not None:
        weights = {"t": 0.40, "f": 0.30, "n": 0.0, "p": 0.30}
    else:
        weights = {"t": 0.55, "f": 0.45, "n": 0.0, "p": 0.0}

    total = (
        technical_score   * weights["t"]
        + fundamental_score * weights["f"]
        + (news_score or 0) * weights["n"]
        + (prediction_score or 0) * weights["p"]
    )

    if news_score is not None:
        news_delta = (news_score - 50) / 50 * 10  # -10 to +10
        total += news_delta

    total += event_delta
    return round(max(0, min(100, total)), 1)


# ── Decision logic ─────────────────────────────────────────────────────────────
def _decide(
    technical_score: int,
    fundamental_score: int,
    news_score: int | None,
    prediction_score: int | None,
    trend_strength: str,
    volume_surge: bool,
    dist_to_resistance_pct: float | None,
    event_risk: bool,
    already_owned: bool,
    combined: float,
    data_insufficient: bool = False,
) -> Decision:
    # 1. Handle newly listed / insufficient price data
    if data_insufficient:
        # Give "WAIT" instead of "DO NOT BUY" if news sentiment is strongly positive
        if news_score is not None and news_score >= 60:
            return Decision.WAIT
        return Decision.DO_NOT_BUY

    # 2. Sell / Hold signals if already owned
    if already_owned and combined < 35:
        return Decision.SELL
    if already_owned and 35 <= combined < 60:
        return Decision.HOLD

    # 3. Buy / Prepare signals if not owned
    news_ok       = news_score is None or news_score >= 35
    model_ok      = prediction_score is None or prediction_score >= 50
    resistance_ok = dist_to_resistance_pct is None or dist_to_resistance_pct > 1

    strong_buy = (
        technical_score >= 60
        and fundamental_score >= 50
        and trend_strength in ("strong", "moderate")
        and volume_surge
        and resistance_ok
        and news_ok
        and model_ok
    )

    if strong_buy:
        return Decision.PREPARE_TO_BUY if event_risk else Decision.BUY_NOW

    if fundamental_score >= 45 and 50 <= technical_score < 60:
        return Decision.PREPARE_TO_BUY

    if already_owned and combined >= 60:
        return Decision.HOLD

    return Decision.DO_NOT_BUY


# ── Main route ─────────────────────────────────────────────────────────────────
@app.get("/decide/{symbol}")
async def decide(symbol: str, already_owned: bool = False):
    try:
        async with httpx.AsyncClient(timeout=70) as client:
            technical_task   = asyncio.create_task(_fetch_optional(client, f"{TECHNICAL_URL}/analyze/{symbol}", "Technical"))
            fundamental_task = asyncio.create_task(_fetch_optional(client, f"{FUNDAMENTAL_URL}/analyze/{symbol}", "Fundamental"))
            news_task        = asyncio.create_task(_fetch_optional(client, f"{NEWS_URL}/analyze/{symbol}", "News"))
            events_task      = asyncio.create_task(_fetch_optional(client, f"{EVENT_URL}/events/{symbol}", "Events"))
            prediction_task  = asyncio.create_task(_fetch_optional(client, f"{PREDICTION_URL}/predict/{symbol}", "Prediction"))

            technical, fundamental, news, events, prediction = await asyncio.gather(
                technical_task, fundamental_task, news_task, events_task, prediction_task
            )

        # ── Extract scores ─────────────────────────────────────────────────────
        data_insufficient = False

        # Technical – default to 50 if missing
        if not technical or not isinstance(technical, dict):
            technical = {
                "technical_score": 50,
                "trend_strength": "unknown",
                "volume_surge": False,
                "close": None, "support": None, "resistance": None,
                "reasons": ["Technical service temporarily unavailable"],
            }
        if technical.get("close") is None:
            data_insufficient = True

        # Fundamental – default to 50 if missing
        if not fundamental or not isinstance(fundamental, dict):
            fundamental = {
                "fundamental_score": 50,
                "valuation": "fair",
                "sector": None,
                "reasons": ["Live data temporarily unavailable — score is based on last known or default values"],
                "metrics": {},
                "fallback_used": True
            }

        technical_score   = int(technical.get("technical_score", 50))
        fundamental_score = int(fundamental.get("fundamental_score", 50))

        # 🔥 SAFE CONVERSION: handle None values
        news_score = None
        if news and "news_score" in news:
            val = news["news_score"]
            if val is not None:
                try:
                    news_score = int(val)
                except (ValueError, TypeError):
                    logger.warning(f"Invalid news_score for {symbol}: {val}")

        prediction_score = None
        if prediction and prediction.get("model_loaded"):
            val = prediction.get("prediction_score")
            if val is not None:
                try:
                    prediction_score = int(val)
                except (ValueError, TypeError):
                    logger.warning(f"Invalid prediction_score for {symbol}: {val}")

        if technical.get("data_insufficient"):
            data_insufficient = True

        # ── Event signals ──────────────────────────────────────────────────────
        event_signals = _extract_event_signals(events)
        event_delta   = event_signals["event_score_delta"]
        event_risk    = event_signals["event_risk"]
        event_reasons = event_signals["event_reasons"]

        # ── Price data ────────────────────────────────────────────────────────
        close      = technical.get("close")
        support    = technical.get("support")
        resistance = technical.get("resistance")
        trend_strength = technical.get("trend_strength", "unknown")
        volume_surge   = bool(technical.get("volume_surge", False))

        dist_to_resistance_pct = None
        if close and resistance and resistance > 0:
            dist_to_resistance_pct = round(((resistance - close) / close) * 100, 2)

        # ── Combined score & decision ─────────────────────────────────────────
        combined = _combined_score(
            technical_score, fundamental_score,
            news_score, prediction_score, event_delta,
        )

        decision = _decide(
            technical_score, fundamental_score,
            news_score, prediction_score,
            trend_strength, volume_surge,
            dist_to_resistance_pct,
            event_risk, already_owned,
            combined, data_insufficient,
        )

        # ── Entry / Target / Stop ─────────────────────────────────────────────
        entry_low = entry_high = target = stop_loss = None
        if close:
            support_val = support if support else close * 0.95
            entry_low  = round(support_val * 1.01, 2)
            entry_high = round(close * 1.005, 2)

            target_pct = 0.08
            if event_signals["earnings_days_out"] is not None:
                d = event_signals["earnings_days_out"]
                if 0 < d <= EARNINGS_BOOST_DAYS:
                    target_pct = 0.12
            if prediction_score is not None:
                target_pct = target_pct * 0.7 + (prediction_score / 100) * 0.05

            target    = round(close * (1 + target_pct), 2)
            stop_loss = round(support_val * 0.98, 2)

        confidence = "High" if combined >= 75 else "Medium" if combined >= 55 else "Low"

        # ── Reasons assembly ──────────────────────────────────────────────────
        reasons: dict = {
            "technical":   technical.get("reasons", []),
            "fundamental": fundamental.get("reasons", []),
        }
        if news and isinstance(news, dict):
            reasons["news"] = news.get("reasons", [])
        if prediction and isinstance(prediction, dict) and prediction.get("model_loaded"):
            reasons["prediction"] = [prediction.get("note", "AI prediction available")]
        if event_reasons:
            reasons["event"] = event_reasons

        response = {
            "symbol":           symbol.upper(),
            "decision":         decision.value,
            "confidence":       confidence,
            "combined_score":   combined,
            "technical_score":  technical_score,
            "fundamental_score": fundamental_score,
            "news_score":       news_score,
            "prediction_score": prediction_score,
            "event_score_delta": event_delta,
            "event_risk":       event_risk,
            "entry_range":      {"low": entry_low, "high": entry_high} if entry_low else None,
            "target":           target,
            "stop_loss":        stop_loss,
            "holding_period":   "2-6 weeks" if decision in [Decision.BUY_NOW, Decision.PREPARE_TO_BUY] else "N/A",
            "close":            close,
            "support":          support,
            "resistance":       resistance,
            "reasons":          reasons,
            "valuation":        fundamental.get("valuation", "fair"),
            "sector":           fundamental.get("sector"),
            "data_insufficient": data_insufficient,
            "fundamental_fallback": fundamental.get("fallback_used", False),
        }

        if news and isinstance(news, dict):
            response["news_data"] = {
                "headline_count": news.get("headline_count", 0),
                "headlines":      news.get("headlines", []),
                "reasons":        news.get("reasons", []),
            }
        if events and isinstance(events, dict):
            response["event_data"] = events

        if fundamental.get("metrics"):
            response["fundamental_metrics"] = fundamental["metrics"]

        return response

    except Exception as e:
        logger.error(f"Decision failed for {symbol}: {e}", exc_info=True)
        # 🛡️ Always return a 200 with fallback values, never crash
        return {
            "symbol": symbol.upper(),
            "decision": "DO NOT BUY",
            "confidence": "Low",
            "combined_score": 0,
            "technical_score": 50,
            "fundamental_score": 50,
            "news_score": None,
            "prediction_score": None,
            "event_risk": False,
            "entry_range": None,
            "target": None,
            "stop_loss": None,
            "holding_period": "N/A",
            "close": None,
            "support": None,
            "resistance": None,
            "reasons": {
                "technical": ["Data unavailable"],
                "fundamental": ["Data unavailable"]
            },
            "valuation": "fair",
            "sector": None,
            "data_insufficient": True,
            "fundamental_metrics": {},
            "fundamental_fallback": True,
        }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8004))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)