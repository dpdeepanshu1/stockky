"""
Prediction Service
--------------------
Single responsibility: estimate the probability that a stock becomes a
high-quality buying opportunity (per the training label: ~5%+ gain within
~10 trading days) — NOT a certainty, a probability. This is the piece of
the product spec that says "estimate probability... whenever confidence is
insufficient, wait" — the Decision Engine treats low model confidence the
same way it treats a weak technical/fundamental score: as a reason to wait,
not a reason to force a call.

If model.pkl hasn't been trained yet (see train.py), this service falls
back to an honest "model not trained" response rather than pretending to
have a number — the Decision Engine is written to treat that as neutral,
not as a false signal.
"""
import os
import logging

import httpx
import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from features import latest_feature_vector, FEATURE_COLUMNS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prediction-service")

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "http://market-data-service:8001")
MODEL_PATH = os.getenv("MODEL_PATH", "model.pkl")

app = FastAPI(title="Stockky Prediction Service", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_model = None
if os.path.exists(MODEL_PATH):
    try:
        _model = joblib.load(MODEL_PATH)
        logger.info("Loaded trained model from %s", MODEL_PATH)
    except Exception as e:
        logger.error("Failed to load model: %s", e)
else:
    logger.warning("No trained model found at %s — run train.py first. Serving fallback responses.", MODEL_PATH)


@app.get("/health")
def health():
    return {"status": "ok", "service": "prediction-service", "model_loaded": _model is not None}


def _fetch_history(symbol: str) -> pd.DataFrame:
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/history/{symbol}", params={"period": "1y"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Market data service unreachable: {e}")

    candles = data.get("candles", [])
    if len(candles) < 210:
        raise HTTPException(status_code=422, detail="Not enough history for prediction (need ~210 trading days)")

    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    df.rename(columns=str.title, inplace=True)
    return df


@app.get("/predict/{symbol}")
def predict(symbol: str):
    if _model is None:
        return {
            "symbol": symbol.upper(),
            "model_loaded": False,
            "probability": None,
            "prediction_score": None,
            "note": "No trained model yet. Run 'docker compose run --rm prediction-service python train.py' to train one. Decision Engine treats this as neutral, not a signal.",
        }

    df = _fetch_history(symbol)
    features = latest_feature_vector(df)

    missing = [c for c in FEATURE_COLUMNS if c not in features]
    if missing:
        raise HTTPException(status_code=422, detail=f"Could not compute all features (missing: {missing}), likely too little history")

    X = pd.DataFrame([features])[FEATURE_COLUMNS]
    probability = float(_model.predict_proba(X)[0, 1])
    prediction_score = round(probability * 100)

    return {
        "symbol": symbol.upper(),
        "model_loaded": True,
        "probability": round(probability, 3),
        "prediction_score": prediction_score,
        "note": f"Estimated {prediction_score}% probability of a ~5%+ move within 10 trading days, based on current technical setup vs 5 years of historical patterns across a 25-stock NSE universe.",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8007, reload=True)
