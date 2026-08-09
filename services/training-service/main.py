"""
Training Service
Responsibility: Record predictions, evaluate outcomes, train models, and provide
a training intelligence signal.
"""
import os
import logging
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any

import joblib
import pandas as pd
import numpy as np
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ✅ Absolute imports (no leading dot)
import models as db_models
from train import train_model
from evaluate import evaluate_t1, evaluate_t5
from scanner import TrainingScanner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("training-service")

# --- Configuration ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./training.db")
MODEL_STORE_PATH = os.getenv("MODEL_STORE_PATH", "./model-store")
os.makedirs(MODEL_STORE_PATH, exist_ok=True)

# --- Database Setup ---
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = db_models.Base

# --- FastAPI App ---
app = FastAPI(title="Stockky Training Service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Data Models ---
class PredictionSnapshot(BaseModel):
    prediction_id: str
    symbol: str
    timestamp: datetime
    price: float
    decision: str
    confidence: float
    entry_range: Optional[str]
    target: Optional[float]
    stop_loss: Optional[float]
    market_sentiment: Dict[str, Any]
    features: Dict[str, Any]

class TrainingScoreResponse(BaseModel):
    symbol: str
    training_score: int  # 0-100
    t1_success_probability: float
    t5_success_probability: float
    historical_similarity: float
    similar_setups: List[Dict]

class ModelStatusResponse(BaseModel):
    production_model: Optional[Dict]
    candidate_model: Optional[Dict]
    last_training_date: Optional[datetime]
    dataset_size: int
    performance: Dict

# --- Startup event: create tables ---
@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created (if missing)")

# --- API Endpoints ---
@app.post("/record-prediction")
async def record_prediction(snapshot: PredictionSnapshot, background_tasks: BackgroundTasks):
    """Record an immutable prediction snapshot for training."""
    db = SessionLocal()
    try:
        # Check if prediction already exists
        existing = db.query(db_models.PredictionSnapshot).filter(
            db_models.PredictionSnapshot.prediction_id == snapshot.prediction_id
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="Prediction already recorded")

        new_snapshot = db_models.PredictionSnapshot(
            prediction_id=snapshot.prediction_id,
            symbol=snapshot.symbol,
            timestamp=snapshot.timestamp,
            price=snapshot.price,
            decision=snapshot.decision,
            confidence=snapshot.confidence,
            entry_range=snapshot.entry_range,
            target=snapshot.target,
            stop_loss=snapshot.stop_loss,
            market_sentiment=snapshot.market_sentiment,
            features=snapshot.features,
            created_at=datetime.now()
        )
        db.add(new_snapshot)
        db.commit()
        logger.info(f"Recorded prediction: {snapshot.prediction_id}")

        # Schedule evaluation
        background_tasks.add_task(evaluate_t1, snapshot.prediction_id)
        background_tasks.add_task(evaluate_t5, snapshot.prediction_id)

        return {"status": "recorded", "prediction_id": snapshot.prediction_id}
    except Exception as e:
        db.rollback()
        logger.error(f"Error recording prediction: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/training-score/{symbol}", response_model=TrainingScoreResponse)
async def get_training_score(symbol: str):
    """Get the training intelligence score for a given symbol."""
    scanner = TrainingScanner(SessionLocal, MODEL_STORE_PATH)
    score = scanner.score_symbol(symbol)
    if not score:
        raise HTTPException(status_code=404, detail="Symbol not found or insufficient data")
    return score

@app.get("/model-status", response_model=ModelStatusResponse)
async def get_model_status():
    """Get the current status of production and candidate models."""
    # Load the latest production model metadata
    prod_path = os.path.join(MODEL_STORE_PATH, "production_model_metadata.json")
    cand_path = os.path.join(MODEL_STORE_PATH, "candidate_model_metadata.json")
    
    production_model = None
    candidate_model = None
    last_training_date = None
    dataset_size = 0
    performance = {}

    if os.path.exists(prod_path):
        with open(prod_path, 'r') as f:
            production_model = json.load(f)
            last_training_date = datetime.fromisoformat(production_model.get('training_date'))
            performance = production_model.get('performance', {})

    if os.path.exists(cand_path):
        with open(cand_path, 'r') as f:
            candidate_model = json.load(f)

    # Get dataset size from DB
    db = SessionLocal()
    try:
        dataset_size = db.query(db_models.PredictionSnapshot).count()
    finally:
        db.close()

    return ModelStatusResponse(
        production_model=production_model,
        candidate_model=candidate_model,
        last_training_date=last_training_date,
        dataset_size=dataset_size,
        performance=performance
    )

@app.post("/train")
async def trigger_training(background_tasks: BackgroundTasks):
    """Trigger a new training run."""
    background_tasks.add_task(train_model, SessionLocal, MODEL_STORE_PATH)
    return {"status": "training_started"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "training-service"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8010))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)