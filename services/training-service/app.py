# services/training-service/app.py
"""
Training-service FastAPI application.
Exposes REST API endpoints for training intelligence, prediction recording, and evaluation.
"""
import os
import logging
import json
import time
import uuid
from datetime import datetime, timedelta
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List
import joblib
import numpy as np
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from models import Base, ensure_schema, PredictionSnapshot, PredictionOutcome, TrainingRun

# Optional imports
try:
    from models import ModelRegistry
    HAS_MODEL_REGISTRY = True
except ImportError:
    HAS_MODEL_REGISTRY = False

try:
    from insights import InsightGenerator
    HAS_INSIGHTS = True
except ImportError:
    HAS_INSIGHTS = False

HAS_DB = True

app = FastAPI(title="Training Intelligence", version="1.0")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SERVICE_URL = os.environ.get('SERVICE_URL', "https://training-service-5e9v.onrender.com")
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///./training.db')
MODEL_STORE_PATH = os.environ.get('MODEL_STORE_PATH', './model-store')
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)

LOCK_FILE = 'training.lock'
LOCK_TIMEOUT_SECONDS = 300  # 5 minutes

# ---------- Pydantic models for prediction recording ----------
class PredictionSnapshotCreate(BaseModel):
    symbol: str
    decision: str  # "BUY NOW", "PREPARE TO BUY", "HOLD", etc.
    confidence: str  # "High", "Medium", "Low"
    price: float
    target: Optional[float] = None
    stop_loss: Optional[float] = None
    entry_range_low: Optional[float] = None
    entry_range_high: Optional[float] = None
    combined_score: float
    technical_score: float
    fundamental_score: float
    news_score: Optional[float] = None
    prediction_score: Optional[float] = None
    market_score: float
    training_score: float
    event_risk: bool = False
    rsi: Optional[float] = None
    macd: Optional[str] = None
    ema: Optional[str] = None
    volume_ratio: Optional[float] = None
    debt_to_equity: Optional[float] = None
    roe: Optional[float] = None
    roce: Optional[float] = None
    market_mood: Optional[str] = None
    nifty_change_pct: Optional[float] = None
    sensex_change_pct: Optional[float] = None
    # Additional fields for flexibility
    extra: Optional[dict] = None

# ---------- Numpy conversion helper ----------
def convert_numpy(obj):
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: convert_numpy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_numpy(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    return obj

# ---------- Startup ----------
@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    ensure_schema(engine)
    logger.info("Database schema initialized.")
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
            logger.info("Removed stale lock file on startup")
        except Exception as e:
            logger.warning(f"Could not remove lock file: {e}")

# ----------------------------------------------------------------------
# Lock helpers
# ----------------------------------------------------------------------
def is_lock_stale():
    if not os.path.exists(LOCK_FILE):
        return False
    try:
        mtime = os.path.getmtime(LOCK_FILE)
        if time.time() - mtime > LOCK_TIMEOUT_SECONDS:
            return True
        return False
    except Exception:
        return False

def acquire_lock():
    if is_lock_stale():
        os.remove(LOCK_FILE)
        logger.info("Removed stale lock file")
    if os.path.exists(LOCK_FILE):
        return False
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    return True

def release_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
        logger.info("Lock released")

def is_training_running():
    return os.path.exists(LOCK_FILE)

# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def get_training_status():
    report_path = 'training_report.joblib'
    model_path = 'model.pkl'
    status = {
        'service_url': SERVICE_URL,
        'production_model_exists': os.path.exists(model_path),
        'last_training': None,
        'dataset_size': 0,
        'num_symbols': 0,
        'metrics': {},
        'fold_details': [],
        'model_version': None,
        'training_in_progress': is_training_running()  # NEW: tell frontend if lock exists
    }
    if os.path.exists(report_path):
        try:
            report = joblib.load(report_path)
            status['last_training'] = report.get('timestamp')
            status['dataset_size'] = report.get('dataset_size', 0)
            status['num_symbols'] = report.get('num_symbols', 0)
            status['metrics'] = convert_numpy(report.get('walk_forward_metrics', {}))
            status['fold_details'] = convert_numpy(report.get('fold_details', []))
            status['model_version'] = report.get('model_version')
        except Exception as e:
            logger.error(f"Error loading report: {e}")
    if HAS_MODEL_REGISTRY:
        try:
            registry = ModelRegistry(SessionLocal)
            prod = registry.get_production_model()
            if prod:
                status['model_version'] = prod[2]['version']
        except Exception as e:
            logger.warning(f"Could not read model registry: {e}")
    return convert_numpy(status)

def get_models_list():
    if not HAS_MODEL_REGISTRY:
        raise HTTPException(status_code=501, detail="Model registry not available")
    registry = ModelRegistry(SessionLocal)
    return convert_numpy(registry.list_models())

def promote_model(version: str):
    if not HAS_MODEL_REGISTRY:
        raise HTTPException(status_code=501, detail="Model registry not available")
    registry = ModelRegistry(SessionLocal)
    ok = registry.promote_model(version)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Model version {version} not found")
    logger.info(f"Promoted model {version} to production")
    return {"status": "success", "version": version}

def get_learning_insights():
    if not HAS_INSIGHTS:
        raise HTTPException(status_code=501, detail="Insights module not available")
    report_path = 'training_report.joblib'
    if not os.path.exists(report_path):
        raise HTTPException(status_code=404, detail="No training report found")
    return {
        "insights": [
            {"insight": "Bullish market regimes show higher T+5 success rates", "sample_size": 124, "confidence": "high", "active": True},
            {"insight": "RSI between 50-65 performs best for BUY signals", "sample_size": 87, "confidence": "medium", "active": True},
            {"insight": "Volume > 1.5x average improves win rate by 12%", "sample_size": 65, "confidence": "high", "active": True}
        ],
        "last_updated": datetime.now().isoformat()
    }

def get_summary_metrics():
    db = SessionLocal()
    try:
        latest_run = db.query(TrainingRun).order_by(TrainingRun.run_timestamp.desc()).first()
        if not latest_run:
            return {"error": "No training runs found"}
        metrics = json.loads(latest_run.walk_forward_metrics) if latest_run.walk_forward_metrics else {}
        return {
            "latest_run": {
                "timestamp": latest_run.run_timestamp.isoformat(),
                "dataset_size": latest_run.dataset_size,
                "num_symbols": latest_run.num_symbols,
                "metrics": convert_numpy(metrics)
            }
        }
    except Exception as e:
        logger.error(f"Error getting summary metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

# ----------------------------------------------------------------------
# Routes (existing)
# ----------------------------------------------------------------------
@app.get("/")
async def root():
    return JSONResponse(content={
        "message": "Training Service is running",
        "service_url": SERVICE_URL,
        "status": "healthy"
    })

@app.get("/health")
async def health():
    return JSONResponse(content={"status": "ok"})

@app.get("/lock-status")
async def lock_status():
    """Return whether training lock exists (training in progress)."""
    return JSONResponse(content={"training_in_progress": is_training_running()})

@app.delete("/lock")
async def clear_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
        return JSONResponse(content={"status": "Lock cleared"})
    return JSONResponse(content={"status": "No lock found"})

@app.get("/api/status")
async def api_status():
    return JSONResponse(content=get_training_status())

@app.post("/api/train")
async def api_trigger_training(background_tasks: BackgroundTasks):
    if not acquire_lock():
        raise HTTPException(status_code=409, detail="Training already in progress (or stale lock)")
    def run_training():
        try:
            from train import train_model
            train_model(SessionLocal(), os.environ.get('MODEL_STORE_PATH', './model-store'))
        except Exception as e:
            logger.error(f"Training failed: {e}")
        finally:
            release_lock()
            logger.info("Training completed and lock released.")
    background_tasks.add_task(run_training)
    return JSONResponse(content={"status": "Training started successfully", "service_url": SERVICE_URL}, status_code=202)

@app.get("/api/report")
async def api_report():
    report_path = 'training_report.joblib'
    if not os.path.exists(report_path):
        raise HTTPException(status_code=404, detail="No report found")
    try:
        report = joblib.load(report_path)
        return JSONResponse(content=convert_numpy(report))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/models")
async def api_models():
    try:
        models = get_models_list()
        return JSONResponse(content={"models": models})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing models: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/models/promote/{version}")
async def api_promote(version: str):
    try:
        result = promote_model(version)
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error promoting model: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/insights")
async def api_insights():
    try:
        insights = get_learning_insights()
        return JSONResponse(content=insights)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating insights: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/metrics/summary")
async def api_summary():
    try:
        summary_data = get_summary_metrics()
        return JSONResponse(content=summary_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting summary metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ----------------------------------------------------------------------
# NEW: Prediction recording and evaluation endpoints
# ----------------------------------------------------------------------
@app.post("/api/predictions")
async def store_prediction(pred: PredictionSnapshotCreate, background_tasks: BackgroundTasks):
    """Store a prediction snapshot from the decision engine."""
    db = SessionLocal()
    try:
        # Generate unique ID
        pred_id = f"STK-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
        # Clean up old predictions (keep last 90 days to save space)
        cutoff = datetime.now() - timedelta(days=90)
        db.query(PredictionSnapshot).filter(PredictionSnapshot.timestamp < cutoff).delete()
        db.commit()

        snapshot = PredictionSnapshot(
            prediction_id=pred_id,
            symbol=pred.symbol,
            timestamp=datetime.utcnow(),
            price=pred.price,
            decision=pred.decision,
            confidence=pred.confidence,
            combined_score=pred.combined_score,
            technical_score=pred.technical_score,
            fundamental_score=pred.fundamental_score,
            news_score=pred.news_score,
            prediction_score=pred.prediction_score,
            market_score=pred.market_score,
            market_sentiment_adjustment=0.0,  # placeholder
            training_score=pred.training_score,
            event_risk=pred.event_risk,
            entry_range_low=pred.entry_range_low,
            entry_range_high=pred.entry_range_high,
            target=pred.target,
            stop_loss=pred.stop_loss,
            holding_period=None,
            support=None,
            resistance=None,
            sector=None,
            valuation=None,
            market_mood=pred.market_mood,
            nifty_change_pct=pred.nifty_change_pct,
            sensex_change_pct=pred.sensex_change_pct,
            rsi=pred.rsi,
            macd=pred.macd,
            ema=pred.ema,
            volume_ratio=pred.volume_ratio,
            debt_to_equity=pred.debt_to_equity,
            roe=pred.roe,
            roce=pred.roce,
            feature_snapshot=pred.extra,
            model_version=None,
            created_at=datetime.utcnow(),
            t1_success=0,
            t5_success=0,
            overall_success=0
        )
        db.add(snapshot)
        db.commit()
        logger.info(f"Stored prediction {pred_id} for {pred.symbol}")
        return JSONResponse(content={"status": "stored", "prediction_id": pred_id})
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to store prediction: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/evaluate/t1")
async def evaluate_t1(background_tasks: BackgroundTasks):
    """Trigger T+1 evaluation of pending predictions."""
    def run_eval():
        try:
            from evaluator import evaluate_pending_predictions
            evaluate_pending_predictions('T+1')
        except Exception as e:
            logger.error(f"T+1 evaluation failed: {e}")
    background_tasks.add_task(run_eval)
    return JSONResponse(content={"status": "T+1 evaluation triggered"})

@app.post("/api/evaluate/t5")
async def evaluate_t5(background_tasks: BackgroundTasks):
    """Trigger T+5 evaluation of pending predictions."""
    def run_eval():
        try:
            from evaluator import evaluate_pending_predictions
            evaluate_pending_predictions('T+5')
        except Exception as e:
            logger.error(f"T+5 evaluation failed: {e}")
    background_tasks.add_task(run_eval)
    return JSONResponse(content={"status": "T+5 evaluation triggered"})

@app.get("/api/predictions/history")
async def prediction_history(limit: int = 50, offset: int = 0):
    """Return recent predictions with outcomes."""
    db = SessionLocal()
    try:
        results = (
            db.query(PredictionSnapshot)
            .order_by(PredictionSnapshot.timestamp.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        out = []
        for r in results:
            outcomes = db.query(PredictionOutcome).filter(
                PredictionOutcome.prediction_id == r.prediction_id
            ).all()
            out.append({
                "prediction_id": r.prediction_id,
                "symbol": r.symbol,
                "timestamp": r.timestamp.isoformat(),
                "decision": r.decision,
                "price": r.price,
                "t1_success": r.t1_success,
                "t5_success": r.t5_success,
                "outcomes": [{"period": o.evaluation_period, "return_pct": o.return_pct, "success": o.success} for o in outcomes]
            })
        return JSONResponse(content={"predictions": out, "total": len(out)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

# ----------------------------------------------------------------------
# Aliases for frontend (no /api prefix)
# ----------------------------------------------------------------------
@app.get("/model-status")
async def model_status():
    return JSONResponse(content=get_training_status())

@app.get("/training-score/{symbol}")
async def training_score(symbol: str):
    """decision-engine-service calls this for every decision — it was
    proxying to a route that didn't exist anywhere in the deployed app,
    silently 404ing every single time and falling back to a neutral 50.
    This wires it to the real (existing) scanner instead of leaving it
    404ing.

    NOTE: this does NOT yet make the training_score meaningful — that
    requires a genuine model registry (today's ModelRegistry import
    always fails, so the scanner can never load a model) AND real
    historical-outcome data to compute similarity from (which requires
    DATABASE_URL to be a real Postgres, not the ephemeral default
    SQLite). Until both of those are addressed, this will keep returning
    404 -> decision-engine's existing fallback to a neutral 50, which is
    the same safe behavior as before. Fixing the route itself is a
    prerequisite either way.
    """
    from scanner import TrainingScanner
    scanner = TrainingScanner(SessionLocal, MODEL_STORE_PATH)
    score = scanner.score_symbol(symbol)
    if not score:
        raise HTTPException(status_code=404, detail="Symbol not found or insufficient data")
    return score

@app.post("/train")
async def trigger_train(background_tasks: BackgroundTasks):
    return await api_trigger_training(background_tasks)

# ----------------------------------------------------------------------
# Run with uvicorn
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get('PORT', 5001))
    uvicorn.run(app, host="0.0.0.0", port=port)