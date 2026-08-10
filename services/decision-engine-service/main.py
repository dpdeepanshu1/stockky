"""
Decision Engine Service v0.7.1
Changes:
- Market Sentiment score is now part of the weighted average (weight 0.10)
- All missing scores default to 50 (neutral)
- Added logging for market sentiment fetch
- Improved error handling for sentiment service
"""
import os
import asyncio
import logging
from datetime import datetime
from enum import Enum
import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("decision-engine-service")

# ---- Service URLs ----
TECHNICAL_URL = os.getenv("TECHNICAL_URL", "https://technical-analysis-service-zhnc.onrender.com")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", "https://fundamental-analysis-service.onrender.com")
NEWS_URL = os.getenv("NEWS_URL", "https://news-intelligence-service.onrender.com")
EVENT_URL = os.getenv("EVENT_URL", "https://event-tracker-service-m1lw.onrender.com")
PREDICTION_URL = os.getenv("PREDICTION_URL", "https://prediction-service-wowb.onrender.com")
MARKET_SENTIMENT_URL = os.getenv("MARKET_SENTIMENT_URL", "https://market-sentiment-service.onrender.com")
TRAINING_SERVICE_URL = os.getenv("TRAINING_SERVICE_URL", "https://training-service-5e9v.onrender.com")

EARNINGS_RISK_DAYS = 3
EARNINGS_BOOST_DAYS = 7

app = FastAPI(title="Stockky Decision Engine", version="0.7.1")
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
    BUY_NOW = "BUY NOW"
    PREPARE_TO_BUY = "PREPARE TO BUY"
    HOLD = "HOLD"
    WAIT = "WAIT"
    DO_NOT_BUY = "DO NOT BUY"
    SELL = "SELL"


@app.get("/")
def root():
    return {"service": "Stockky Decision Engine", "version": "0.7.1", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "decision-engine-service"}


# ── Fetch helpers ──────────────────────────────────────────────────
async def _fetch_optional(client: httpx.AsyncClient, url: str, label: str):
    try:
        resp = await client.get(url, timeout=70)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        logger.warning("%s unavailable: %s", label, e)
        return None


# ── Market Sentiment fetch with logging ────────────────────────────
async def get_market_sentiment() -> dict:
    """Fetch current market sentiment; return neutral fallback on failure."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{MARKET_SENTIMENT_URL}/sentiment")
            if resp.status_code == 200:
                data = resp.json()
                score = data.get("market_score", 50)
                logger.info(f"Market sentiment fetched: {score}")
                return {"market_score": score, **data}
            else:
                logger.warning(f"Market sentiment returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Could not fetch market sentiment: {e}")
    return {"market_score": 50, "classification": "NEUTRAL", "trend": "Neutral"}


# ── Training Intelligence fetch ──────────────────────────────────────
async def get_training_score(symbol: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{TRAINING_SERVICE_URL}/training-score/{symbol}")
            if resp.status_code == 200:
                data = resp.json()
                return data
            else:
                logger.warning(f"Training score for {symbol} returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Could not fetch training score for {symbol}: {e}")
    return {
        "symbol": symbol,
        "training_score": 50,
        "t1_success_probability": 0.5,
        "t5_success_probability": 0.5,
        "historical_similarity": 0.5,
        "similar_setups": []
    }


# ── Event signal extraction ────────────────────────────────────
def _extract_event_signals(events: dict | None) -> dict:
    if not events or not isinstance(events, dict):
        return {"event_score_delta": 0, "event_risk": False, "event_reasons": [], "earnings_days_out": None}

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
                reasons.append(f"📈 Earnings in {days_out}d — pre-results momentum window")
            elif days_out < 0 and days_out >= -30:
                reasons.append(f"📊 Recent earnings ({abs(days_out)}d ago)")
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
                reasons.append(f"🏦 Insider buying: {txn.get('insider', 'insider')} bought {shares:,} shares")
                break
        elif "sell" in txn_type and "sale" in txn_type:
            delta -= 3
            reasons.append(f"🏦 Insider selling: {txn.get('insider', 'insider')} sold shares")
            break

    # Bulk/Block deals
    bulk_deals = events.get("bulk_deals") or []
    if bulk_deals:
        delta += 4
        reasons.append(f"📦 Bulk/Block deal detected")

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


# ── Market Sentiment Adjustment ──────────────────────────────
def _market_sentiment_adjustment(market_score: int) -> tuple:
    if market_score >= 70:
        return (8, f"📈 Very strong bullish market sentiment (+8)")
    elif market_score >= 60:
        bonus = int((market_score - 60) / 10 * 8)
        return (bonus, f"📈 Positive market sentiment (+{bonus})")
    elif market_score <= 30:
        return (-8, f"📉 Very strong bearish market sentiment (-8)")
    elif market_score <= 40:
        penalty = int((40 - market_score) / 10 * 8)
        return (-penalty, f"📉 Negative market sentiment (-{penalty})")
    else:
        return (0, f"➖ Neutral market sentiment (no adjustment)")


# ── Combined score (with market sentiment as a component) ──────────
def _combined_score(
    technical_score: int,
    fundamental_score: int,
    news_score: int | None,
    prediction_score: int | None,
    training_score: int,
    market_score: int,
    event_delta: int = 0,
    market_adjustment: int = 0,
) -> float:
    # Default missing values to 50
    news = news_score if news_score is not None else 50
    pred = prediction_score if prediction_score is not None else 50

    weights = {
        "t": 0.30,
        "f": 0.20,
        "n": 0.15,
        "p": 0.15,
        "m": 0.10,
        "train": 0.10,
    }

    total = (
        technical_score * weights["t"] +
        fundamental_score * weights["f"] +
        news * weights["n"] +
        pred * weights["p"] +
        market_score * weights["m"] +
        training_score * weights["train"]
    )

    total += event_delta + market_adjustment
    return round(max(0, min(100, total)), 1)


# ── Decision logic ──────────────────────────────────────────
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
    if data_insufficient:
        if news_score is not None and news_score >= 60:
            return Decision.WAIT
        return Decision.DO_NOT_BUY

    if already_owned and combined < 35:
        return Decision.SELL
    if already_owned and 35 <= combined < 60:
        return Decision.HOLD

    news_ok = news_score is None or news_score >= 35
    model_ok = prediction_score is None or prediction_score >= 50
    resistance_ok = dist_to_resistance_pct is None or dist_to_resistance_pct > 1

    strong_buy = (
        technical_score >= 60 and
        fundamental_score >= 50 and
        trend_strength in ("strong", "moderate") and
        volume_surge and
        resistance_ok and
        news_ok and
        model_ok
    )
    if strong_buy:
        return Decision.PREPARE_TO_BUY if event_risk else Decision.BUY_NOW

    if fundamental_score >= 45 and 50 <= technical_score < 60:
        return Decision.PREPARE_TO_BUY

    if already_owned and combined >= 60:
        return Decision.HOLD

    return Decision.DO_NOT_BUY


# ── Record prediction to Training Service ────────────────────────────
async def record_prediction_for_training(
    symbol: str,
    decision: str,
    confidence: float,
    price: float,
    entry_range: dict,
    target: float,
    stop_loss: float,
    market_sentiment: dict,
    features: dict,
    event_data: dict | None = None,
    fundamental_metrics: dict | None = None,
):
    payload = {
        "symbol": symbol,
        "decision": decision,
        "confidence": "High" if confidence >= 75 else "Medium" if confidence >= 55 else "Low",
        "price": price,
        "combined_score": confidence,
        "technical_score": features.get("technical", 50),
        "fundamental_score": features.get("fundamental", 50),
        "news_score": features.get("news"),
        "prediction_score": features.get("prediction"),
        "market_score": features.get("market", 50),
        "market_sentiment_adjustment": features.get("market_adjustment", 0),
        "training_score": features.get("training", 50),
        "event_risk": features.get("event_risk", False),
        "entry_range_low": entry_range.get("low") if entry_range else None,
        "entry_range_high": entry_range.get("high") if entry_range else None,
        "target": target,
        "stop_loss": stop_loss,
        "holding_period": "2-6 weeks",
        "support": features.get("support"),
        "resistance": features.get("resistance"),
        "sector": None,
        "valuation": "fair",
        "market_mood": market_sentiment.get("classification", "NEUTRAL"),
        "nifty_change_pct": market_sentiment.get("nifty_change_pct"),
        "sensex_change_pct": market_sentiment.get("sensex_change_pct"),
        "rsi": None,
        "macd": None,
        "ema": None,
        "volume_ratio": None,
        "debt_to_equity": fundamental_metrics.get("debt_to_equity") if fundamental_metrics else None,
        "roe": fundamental_metrics.get("roe") if fundamental_metrics else None,
        "roce": fundamental_metrics.get("roce") if fundamental_metrics else None,
        "feature_snapshot": features,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{TRAINING_SERVICE_URL}/api/predictions",
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            if response.status_code in (200, 201):
                logger.info(f"Prediction recorded for {symbol}: {response.json().get('prediction_id')}")
            else:
                logger.warning(f"Failed to record prediction: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Error recording prediction for {symbol}: {e}")


# ── Main route ────────────────────────────────────────────────────
@app.get("/decide/{symbol}")
async def decide(symbol: str, already_owned: bool = False, background_tasks: BackgroundTasks = None):
    try:
        async with httpx.AsyncClient(timeout=70) as client:
            technical_task = asyncio.create_task(_fetch_optional(client, f"{TECHNICAL_URL}/analyze/{symbol}", "Technical"))
            fundamental_task = asyncio.create_task(_fetch_optional(client, f"{FUNDAMENTAL_URL}/analyze/{symbol}", "Fundamental"))
            news_task = asyncio.create_task(_fetch_optional(client, f"{NEWS_URL}/analyze/{symbol}", "News"))
            events_task = asyncio.create_task(_fetch_optional(client, f"{EVENT_URL}/events/{symbol}", "Events"))
            prediction_task = asyncio.create_task(_fetch_optional(client, f"{PREDICTION_URL}/predict/{symbol}", "Prediction"))
            sentiment_task = asyncio.create_task(get_market_sentiment())
            training_task = asyncio.create_task(get_training_score(symbol))

            technical, fundamental, news, events, prediction, sentiment, training = await asyncio.gather(
                technical_task, fundamental_task, news_task, events_task,
                prediction_task, sentiment_task, training_task
            )

        data_insufficient = False

        if not technical or not isinstance(technical, dict):
            technical = {
                "technical_score": 50,
                "trend_strength": "unknown",
                "volume_surge": False,
                "close": None,
                "support": None,
                "resistance": None,
                "reasons": ["Technical service temporarily unavailable"],
            }
        if technical.get("close") is None:
            data_insufficient = True

        if not fundamental or not isinstance(fundamental, dict):
            fundamental = {
                "fundamental_score": 50,
                "valuation": "fair",
                "sector": None,
                "reasons": ["Live data temporarily unavailable — score is based on last known or default values"],
                "metrics": {},
                "fallback_used": True
            }

        technical_score = int(technical.get("technical_score", 50))
        fundamental_score = int(fundamental.get("fundamental_score", 50))

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

        market_score = sentiment.get("market_score", 50)
        training_score = training.get("training_score", 50)

        # Log market sentiment for debugging
        logger.info(f"Market sentiment for {symbol}: {market_score}")

        market_adjustment, market_adjustment_reason = _market_sentiment_adjustment(market_score)

        event_signals = _extract_event_signals(events)
        event_delta = event_signals["event_score_delta"]
        event_risk = event_signals["event_risk"]
        event_reasons = event_signals["event_reasons"]

        close = technical.get("close")
        support = technical.get("support")
        resistance = technical.get("resistance")
        trend_strength = technical.get("trend_strength", "unknown")
        volume_surge = bool(technical.get("volume_surge", False))
        dist_to_resistance_pct = None
        if close and resistance and resistance > 0:
            dist_to_resistance_pct = round(((resistance - close) / close) * 100, 2)

        # Combined score includes market_score
        combined = _combined_score(
            technical_score,
            fundamental_score,
            news_score,
            prediction_score,
            training_score,
            market_score,
            event_delta,
            market_adjustment,
        )

        decision = _decide(
            technical_score,
            fundamental_score,
            news_score,
            prediction_score,
            trend_strength,
            volume_surge,
            dist_to_resistance_pct,
            event_risk,
            already_owned,
            combined,
            data_insufficient,
        )

        entry_low = entry_high = target = stop_loss = None
        if close:
            support_val = support if support else close * 0.95
            entry_low = round(support_val * 1.01, 2)
            entry_high = round(close * 1.005, 2)
            target_pct = 0.08
            if event_signals["earnings_days_out"] is not None:
                d = event_signals["earnings_days_out"]
                if 0 < d <= EARNINGS_BOOST_DAYS:
                    target_pct = 0.12
            if prediction_score is not None:
                target_pct = target_pct * 0.7 + (prediction_score / 100) * 0.05
            target = round(close * (1 + target_pct), 2)
            stop_loss = round(support_val * 0.98, 2)

        confidence = "High" if combined >= 75 else "Medium" if combined >= 55 else "Low"

        reasons: dict = {
            "technical": technical.get("reasons", []),
            "fundamental": fundamental.get("reasons", []),
        }
        if news and isinstance(news, dict):
            reasons["news"] = news.get("reasons", [])
        if prediction and isinstance(prediction, dict) and prediction.get("model_loaded"):
            reasons["prediction"] = [prediction.get("note", "AI prediction available")]
        if event_reasons:
            reasons["event"] = event_reasons
        reasons["market"] = [market_adjustment_reason]
        reasons["training"] = [f"Training intelligence score: {training_score}/100"]

        response = {
            "symbol": symbol.upper(),
            "decision": decision.value,
            "confidence": confidence,
            "combined_score": combined,
            "technical_score": technical_score,
            "fundamental_score": fundamental_score,
            "news_score": news_score,
            "prediction_score": prediction_score,
            "market_score": market_score,
            "market_sentiment_adjustment": market_adjustment,
            "training_score": training_score,
            "event_score_delta": event_delta,
            "event_risk": event_risk,
            "entry_range": {"low": entry_low, "high": entry_high} if entry_low else None,
            "target": target,
            "stop_loss": stop_loss,
            "holding_period": "2-6 weeks" if decision in [Decision.BUY_NOW, Decision.PREPARE_TO_BUY] else "N/A",
            "close": close,
            "support": support,
            "resistance": resistance,
            "reasons": reasons,
            "valuation": fundamental.get("valuation", "fair"),
            "sector": fundamental.get("sector"),
            "data_insufficient": data_insufficient,
            "fundamental_fallback": fundamental.get("fallback_used", False),
        }

        if news and isinstance(news, dict):
            response["news_data"] = {
                "headline_count": news.get("headline_count", 0),
                "headlines": news.get("headlines", []),
                "reasons": news.get("reasons", []),
            }
        if events and isinstance(events, dict):
            response["event_data"] = events
        if fundamental.get("metrics"):
            response["fundamental_metrics"] = fundamental["metrics"]

        # Record prediction for training
        if decision in (Decision.BUY_NOW, Decision.PREPARE_TO_BUY) and close:
            background_tasks.add_task(
                record_prediction_for_training,
                symbol=symbol.upper(),
                decision=decision.value,
                confidence=combined,
                price=close,
                entry_range={"low": entry_low, "high": entry_high},
                target=target,
                stop_loss=stop_loss,
                market_sentiment=sentiment,
                features={
                    "technical": technical_score,
                    "fundamental": fundamental_score,
                    "news": news_score,
                    "prediction": prediction_score,
                    "market": market_score,
                    "market_adjustment": market_adjustment,
                    "training": training_score,
                    "event_delta": event_delta,
                    "event_risk": event_risk,
                    "support": support,
                    "resistance": resistance,
                    "volume_surge": volume_surge,
                    "trend_strength": trend_strength,
                },
                event_data=events,
                fundamental_metrics=fundamental.get("metrics")
            )

        return response

    except Exception as e:
        logger.error(f"Decision failed for {symbol}: {e}", exc_info=True)
        return {
            "symbol": symbol.upper(),
            "decision": Decision.DO_NOT_BUY.value,
            "confidence": "Low",
            "combined_score": 0,
            "technical_score": 50,
            "fundamental_score": 50,
            "news_score": None,
            "prediction_score": None,
            "market_score": 50,
            "market_sentiment_adjustment": 0,
            "training_score": 50,
            "event_score_delta": 0,
            "event_risk": False,
            "entry_range": None,
            "target": None,
            "stop_loss": None,
            "holding_period": "N/A",
            "close": None,
            "support": None,
            "resistance": None,
            "reasons": {
                "technical": ["Error processing request"],
                "fundamental": ["Error processing request"],
                "market": ["Market sentiment unavailable"]
            },
            "valuation": "fair",
            "sector": None,
            "data_insufficient": True,
            "fundamental_fallback": True,
        }