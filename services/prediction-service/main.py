"""
Prediction Service - GenAI Enhanced (512MB Optimized)
--------------------
Uses TinyLlama 1.1B GGML (Q4_0) via llama-cpp-python 0.1.78 (stable generic CPU build).
"""
import os
import logging

import httpx
import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from llama_cpp import Llama
from huggingface_hub import hf_hub_download

from features import latest_feature_vector, FEATURE_COLUMNS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prediction-service")

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "http://market-data-service:8001")
MODEL_PATH = os.getenv("MODEL_PATH", "model.pkl")

app = FastAPI(title="Stockky Prediction Service", version="0.3.0-llama")
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

# --- Load Lightweight GenAI Model (TinyLlama 1.1B Q4_0 GGML, ~220MB RAM) ---
_llm = None
try:
    logger.info("Loading GenAI model (TinyLlama)...")
    
    # Locate the cached model (downloaded during build)
    model_path = hf_hub_download(
        repo_id="TheBloke/TinyLlama-1.1B-Chat-v1.0-GGML",
        filename="tinyllama-1.1b-chat-v1.0.Q4_0.bin"
    )
    
    # Verify the file exists and is not empty
    if not os.path.exists(model_path) or os.path.getsize(model_path) == 0:
        raise FileNotFoundError(f"Model file missing or empty: {model_path}")
    
    # Load the model with safe CPU parameters (n_threads=1 prevents threading issues on Render)
    _llm = Llama(
        model_path=model_path,
        n_ctx=512,
        n_threads=1,
        verbose=False
    )
    logger.info("GenAI model loaded successfully!")
except Exception as e:
    logger.warning(f"Could not load GenAI model: {repr(e)}. Falling back to templated strings.")


@app.get("/")
async def root():
    return {
        "service": "Stockky Prediction Service",
        "version": "0.3.0-llama",
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
        return f"Estimated {round(probability * 100)}% probability of a ~5%+ move within 10 trading days."
    
    rsi = int(feature_dict.get('rsi', 50))
    adx = int(feature_dict.get('adx', 20))
    price_vs_200ema = "above" if feature_dict.get('price_vs_200ema', 0) > 0 else "below"
    
    prompt = f"<|system|>You are an expert stock market analyst. Explain in Hinglish.</s><|user|>RSI is {rsi}. ADX is {adx}. Price is {price_vs_200ema} 200 EMA. Probability {round(probability * 100)}%. Explain why stock may move.</s><|assistant|>"
    
    try:
        res = _llm(prompt, max_tokens=60, temperature=0.7, stop=["</s>", "<|"])
        return res['choices'][0]['text'].strip()
    except Exception as e:
        logger.warning(f"GenAI generation failed: {repr(e)}")
        return f"Estimated {round(probability * 100)}% probability of a ~5%+ move within 10 trading days."

@app.get("/predict/{symbol}")
def predict(symbol: str):
    if _model is None:
        return {
            "symbol": symbol.upper(),
            "model_loaded": False,
            "probability": None,
            "prediction_score": None,
            "note": "No trained model yet.",
        }

    df = _fetch_history(symbol)
    features = latest_feature_vector(df)

    missing = [c for c in FEATURE_COLUMNS if c not in features]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing features: {missing}")

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
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8007)), reload=True)