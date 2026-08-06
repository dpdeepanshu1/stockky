"""
Decision Engine Service
-------------------------
Single responsibility: combine Technical + Fundamental + News + Prediction
scores (and Event risk) into exactly one of five outputs. This is the only
service allowed to say the words "BUY", "SELL", "HOLD", or "DO NOT BUY" —
every other service only produces scores and reasons.

Design intent, per the product spec:
  - No "maybe" outputs. Every symbol gets exactly one clear decision.
  - Waiting is a valid, first-class decision — not a fallback for "unsure".
  - Conviction must be earned: BUY NOW requires technical AND fundamental
    strength AND confirmation (volume/trend) AND non-negative news AND
    (if the model is trained) model support — not just one good number.
  - Event risk (e.g. earnings due in the next 3 days) is a caution flag
    that downgrades BUY NOW to PREPARE TO BUY, not a score — the spec's
    "upcoming event detected" sits earlier in the AI Decision Flow than
    the final confirmation step, so it acts as a gate, not an average.

Phase 2 additions are all optional dependencies: if News, Event, or
Prediction services are unreachable or the model isn't trained yet, the
Decision Engine degrades gracefully to Phase 1 behavior (technical +
fundamental only) rather than failing the whole request. This matters
because these are the three services most likely to be mid-build as you
extend the platform.
"""
import os
import logging
from enum import Enum

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("decision-engine-service")

TECHNICAL_URL = os.getenv("TECHNICAL_URL", "https://technical-analysis-service-zhnc.onrender.com")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", "https://fundamental-analysis-service.onrender.com")
NEWS_URL = os.getenv("NEWS_URL", "https://news-intelligence-service.onrender.com")
EVENT_URL = os.getenv("EVENT_URL", "https://event-tracker-service-m1lw.onrender.com")
PREDICTION_URL = os.getenv("PREDICTION_URL", "https://prediction-service-wowb.onrender.com")

EVENT_RISK_WINDOW_DAYS = 3  # downgrade BUY NOW if earnings are this close

app = FastAPI(title="Stockky Decision Engine", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class Decision(str, Enum):
    BUY_NOW = "BUY NOW"
    PREPARE_TO_BUY = "PREPARE TO BUY"
    HOLD = "HOLD"
    DO_NOT_BUY = "DO NOT BUY"
    SELL = "SELL"


class DecisionRequest(BaseModel):
    already_owned: bool = False


@app.get("/health")
def health():
    return {"status": "ok", "service": "decision-engine-service"}


def _fetch_optional(client: httpx.Client, url: str, label: str) -> dict | None:
    """Fetch from an optional Phase-2 service. Returns None (not an
    exception) if it's unreachable, so the Decision Engine still works
    with just Technical + Fundamental while you're building the rest."""
    try:
        resp = client.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        logger.warning("%s unavailable, continuing without it: %s", label, e)
        return None


def _combined_score(
    technical_score: int,
    fundamental_score: int,
    news_score: int | None,
    prediction_score: int | None,
) -> float:
    # Base weights when only Technical + Fundamental are available (Phase 1
    # parity). News and Prediction are folded in proportionally when present,
    # so the score never silently drops in quality when a service is down —
    # it just relies more on what it has.
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


def _decide(
    technical_score: int,
    fundamental_score: int,
    news_score: int | None,
    prediction_score: int | None,
    trend_strength: str,
    volume_surge: bool,
    dist_to_resistance_pct: float,
    event_risk: bool,
    already_owned: bool,
) -> Decision:
    combined = _combined_score(technical_score, fundamental_score, news_score, prediction_score)

    # SELL: deteriorating setup on a position already owned.
    if already_owned and combined < 40:
        return Decision.SELL

    # HOLD only applies if the user already owns it and things are fine-but-not-a-fresh-buy.
    if already_owned and 40 <= combined < 70:
        return Decision.HOLD

    news_ok = news_score is None or news_score >= 40   # neutral-or-better; don't buy into bad news
    model_ok = prediction_score is None or prediction_score >= 55  # if trained, model must agree

    # BUY NOW: high conviction on every leg that's available, trend confirmed,
    # not chasing into resistance, no negative news, model agrees if trained,
    # and no earnings landmine in the next few days.
    if (
        technical_score >= 70
        and fundamental_score >= 60
        and trend_strength == "strong"
        and volume_surge
        and dist_to_resistance_pct > 2
        and news_ok
        and model_ok
    ):
        if event_risk:
            # Thesis is strong but an earnings date is imminent — the spec
            # treats "upcoming event" as an earlier pipeline stage than the
            # final buy trigger, so we wait for it to clear rather than
            # ignore it.
            return Decision.PREPARE_TO_BUY
        return Decision.BUY_NOW

    # PREPARE TO BUY: thesis is forming (good fundamentals, technicals
    # improving) but confirmation (volume/breakout) hasn't arrived, or news/
    # model aren't yet supportive — wait, don't chase.
    if fundamental_score >= 60 and 55 <= technical_score < 70:
        return Decision.PREPARE_TO_BUY

    if already_owned and combined >= 70:
        return Decision.HOLD

    return Decision.DO_NOT_BUY


@app.get("/decide/{symbol}")
def decide(symbol: str, already_owned: bool = False):
    with httpx.Client(timeout=20) as client:
        try:
            tech_resp = client.get(f"{TECHNICAL_URL}/analyze/{symbol}")
            tech_resp.raise_for_status()
            technical = tech_resp.json()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Technical analysis unavailable: {e}")

        try:
            fund_resp = client.get(f"{FUNDAMENTAL_URL}/analyze/{symbol}")
            fund_resp.raise_for_status()
            fundamental = fund_resp.json()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Fundamental analysis unavailable: {e}")

        news = _fetch_optional(client, f"{NEWS_URL}/analyze/{symbol}", "News Intelligence")
        prediction = _fetch_optional(client, f"{PREDICTION_URL}/predict/{symbol}", "Prediction Service")
        events = _fetch_optional(client, f"{EVENT_URL}/events/{symbol}", "Event Tracker")

    technical_score = technical["technical_score"]
    fundamental_score = fundamental["fundamental_score"]
    news_score = news["news_score"] if news else None
    prediction_score = prediction["prediction_score"] if prediction and prediction.get("model_loaded") else None

    close = technical["close"]
    resistance = technical["resistance"]
    support = technical["support"]
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

    decision = _decide(
        technical_score=technical_score,
        fundamental_score=fundamental_score,
        news_score=news_score,
        prediction_score=prediction_score,
        trend_strength=technical["trend_strength"],
        volume_surge=technical["volume_surge"],
        dist_to_resistance_pct=dist_to_resistance_pct,
        event_risk=event_risk,
        already_owned=already_owned,
    )

    combined_score = _combined_score(technical_score, fundamental_score, news_score, prediction_score)

    # Entry/target/stop-loss: transparent rule-based defaults. If the model
    # is trained, nudge the target using its confidence — still bounded and
    # explainable, not a black box number.
    entry_low, entry_high = round(support * 1.01, 2), round(close * 1.005, 2)
    target_pct = 0.08
    if prediction_score is not None:
        target_pct = 0.06 + (prediction_score / 100) * 0.05  # 6%-11% range scaled by model confidence
    target = round(close * (1 + target_pct), 2)
    stop_loss = round(support * 0.98, 2)

    confidence = "High" if combined_score >= 75 else "Medium" if combined_score >= 55 else "Low"

    reasons = {
        "technical": technical["reasons"],
        "fundamental": fundamental["reasons"],
    }
    if news:
        reasons["news"] = news["reasons"]
    if prediction and prediction.get("model_loaded"):
        reasons["prediction"] = [prediction["note"]]
    if event_reason:
        reasons["event"] = [event_reason]

    return {
        "symbol": symbol.upper(),
        "decision": decision.value,
        "confidence": confidence,
        "combined_score": combined_score,
        "technical_score": technical_score,
        "fundamental_score": fundamental_score,
        "news_score": news_score,
        "prediction_score": prediction_score,
        "event_risk": event_risk,
        "entry_range": {"low": entry_low, "high": entry_high},
        "target": target,
        "stop_loss": stop_loss,
        "holding_period": "2-6 weeks" if decision == Decision.BUY_NOW else "N/A",
        "close": close,
        "support": support,
        "resistance": resistance,
        "reasons": reasons,
        "valuation": fundamental["valuation"],
        "sector": fundamental["sector"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8004, reload=True)
