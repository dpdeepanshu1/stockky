"""
Prediction Service - GenAI Enhanced
--------------------
Estimates probability of price movement using XGBoost, and now uses
a tiny Free GenAI model to interpret the technicals into a natural language summary.
"""
import os
import logging

import httpx
import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from transformers import pipeline # <--- GenAI Import

from features import latest_feature_vector, FEATURE_COLUMNS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prediction-service")

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "http://market-data-service:8001")
MODEL_PATH = os.getenv("MODEL_PATH", "model.pkl")

app = FastAPI(title="Stockky Prediction Service", version="0.2.0-genai")
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

# --- Load GenAI Text Model (DistilGPT2) ---
_llm = None
try:
    logger.info("Loading Free GenAI model (distilgpt2)...")
    # distilgpt2 is only ~82MB and runs safely on CPU
    _llm = pipeline("text-generation", model="distilgpt2", device_map="cpu", max_new_tokens=50, do_sample=True)
    logger.info("GenAI model loaded successfully!")
except Exception as e:
    logger.warning(f"Could not load GenAI model due to memory constraints: {e}. Falling back to templated strings.")


@app.get("/")
async def root():
    return {
        "service": "Stockky Prediction Service",
        "version": "0.2.0-genai",
        "status": "running",
        "model_loaded": _model is not None,
        "genai_loaded": _llm is not None,
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "prediction-service", "model_loaded": _model is not None, "genai_loaded": _llm is not None}


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

def _generate_llm_note(feature_dict: dict, probability: float) -> str:
    if _llm is None:
        return f"Estimated {round(probability * 100)}% probability of a ~5%+ move within 10 trading days, based on current technical setup vs 5 years of historical patterns across a 25-stock NSE universe."
    
    # Build a prompt for the GenAI model based on current features
    # It receives the raw indicators like RSI, MACD, Price position
    rsi = int(feature_dict.get('rsi', 50))
    adx = int(feature_dict.get('adx', 20))
    price_vs_200ema = "above" if feature_dict.get('price_vs_200ema', 0) > 0 else "below"
    
    prompt = f"RSI {rsi} and ADX {adx}. Price is {price_vs_200ema} 200 EMA. Probability {round(probability * 100)}%. Explain in Hinglish why this stock may move."
    
    try:
        res = _llm(prompt, max_new_tokens=60)[0]['generated_text']
        # Clean up the result to remove the prompt itself
        return res.replace(prompt, "").strip()
    except Exception as e:
        logger.warning(f"GenAI generation failed: {e}")
        return f"Estimated {round(probability * 100)}% probability of a ~5%+ move within 10 trading days, based on current technical setup."

@app.get("/predict/{symbol}")
def predict(symbol: str):
    if _model is None:
        return {
            "symbol": symbol.upper(),
            "model_loaded": False,
            "probability": None,
            "prediction_score": None,
            "note": "No trained model yet. Run 'docker compose run --rm prediction-service python train.py' to train one.",
        }

    df = _fetch_history(symbol)
    features = latest_feature_vector(df)

    missing = [c for c in FEATURE_COLUMNS if c not in features]
    if missing:
        raise HTTPException(status_code=422, detail=f"Could not compute all features (missing: {missing}), likely too little history")

    X = pd.DataFrame([features])[FEATURE_COLUMNS]
    probability = float(_model.predict_proba(X)[0, 1])
    prediction_score = round(probability * 100)
    llm_note = _generate_llm_note(features, probability)

    return {
        "symbol": symbol.upper(),
        "model_loaded": True,
        "probability": round(probability, 3),
        "prediction_score": prediction_score,
        "note": llm_note,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8007, reload=True)