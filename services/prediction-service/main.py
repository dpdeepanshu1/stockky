"""
Prediction Service - API-Powered GenAI
--------------------
Uses Groq's free Llama3 API for Hinglish explanations.
Eliminates local model compilation and memory issues on Render.
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

# Get your free API key from console.groq.com
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

app = FastAPI(title="Stockky Prediction Service", version="0.4.0-groq")
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

@app.get("/")
async def root():
    return {"service": "Stockky Prediction Service", "version": "0.4.0-groq", "status": "running", "groq_configured": bool(GROQ_API_KEY)}

@app.get("/health")
def health():
    return {"status": "ok", "service": "prediction-service", "groq_configured": bool(GROQ_API_KEY)}

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
    if not GROQ_API_KEY:
        return f"Estimated {round(probability * 100)}% probability of a ~5%+ move within 10 trading days."
    
    rsi = int(feature_dict.get('rsi', 50))
    adx = int(feature_dict.get('adx', 20))
    price_vs_200ema = "above" if feature_dict.get('price_vs_200ema', 0) > 0 else "below"
    
    system_prompt = "You are an expert stock market analyst. Provide a brief, insightful Hinglish explanation based on the technical indicators provided."
    user_prompt = f"RSI is {rsi}. ADX is {adx}. Price is {price_vs_200ema} 200 EMA. Probability is {round(probability * 100)}%. Explain why the stock may move."
    
    try:
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "llama3-8b-8192",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 60
        }
        resp = httpx.post("https://api.groq.com/openai/v1/chat/completions", json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data['choices'][0]['message']['content'].strip()
        else:
            logger.warning(f"Groq API error: {resp.status_code}")
            return f"Estimated {round(probability * 100)}% probability of a ~5%+ move within 10 trading days."
    except Exception as e:
        logger.warning(f"Groq generation failed: {repr(e)}")
        return f"Estimated {round(probability * 100)}% probability of a ~5%+ move within 10 trading days."

@app.get("/predict/{symbol}")
def predict(symbol: str):
    if _model is None:
        return {"symbol": symbol.upper(), "model_loaded": False, "probability": None, "prediction_score": None, "note": "No trained model yet."}
    df = _fetch_history(symbol)
    features = latest_feature_vector(df)
    missing = [c for c in FEATURE_COLUMNS if c not in features]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing features: {missing}")
    X = pd.DataFrame([features])[FEATURE_COLUMNS]
    probability = float(_model.predict_proba(X)[0, 1])
    prediction_score = round(probability * 100)
    llm_note = _generate_llm_note(features, probability)
    return {"symbol": symbol.upper(), "model_loaded": True, "probability": round(probability, 3), "prediction_score": prediction_score, "note": llm_note}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8007)), reload=True)