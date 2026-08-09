# services/training-service/app.py
"""
Training-service FastAPI application.
Serves the training dashboard and exposes endpoints to trigger and monitor training,
list and promote models, and retrieve learning insights.
"""
import os
import subprocess
import logging
import json
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
import joblib
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import our models and helpers
from models import Base, ensure_schema, PredictionSnapshot, PredictionOutcome, TrainingRun

# Optional imports for enhanced functionality
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

# Database is always available because we use SQLAlchemy
HAS_DB = True

app = FastAPI(title="Training Intelligence", version="1.0")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Templates (ensure you have a 'templates' folder)
templates = Jinja2Templates(directory="templates")

# Service URL – can be overridden by environment variable
SERVICE_URL = os.environ.get('SERVICE_URL', "https://training-service-5e9v.onrender.com")

# ----------------------------------------------------------------------
# Database setup
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///./training.db')
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)

@app.on_event("startup")
def startup():
    """Create tables and apply migrations on startup."""
    Base.metadata.create_all(engine)   # creates tables if they don't exist
    ensure_schema(engine)              # add missing columns if needed
    logger.info("Database schema initialized.")

# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def get_training_status():
    """
    Return a dict with current training status.
    This is the primary status endpoint used by the dashboard.
    """
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
        'model_version': None
    }

    if os.path.exists(report_path):
        try:
            report = joblib.load(report_path)
            status['last_training'] = report.get('timestamp')
            status['dataset_size'] = report.get('dataset_size', 0)
            status['num_symbols'] = report.get('num_symbols', 0)
            status['metrics'] = report.get('walk_forward_metrics', {})
            status['fold_details'] = report.get('fold_details', [])
            status['model_version'] = report.get('model_version')
        except Exception as e:
            logger.error(f"Error loading report: {e}")

    if HAS_MODEL_REGISTRY:
        try:
            registry = ModelRegistry()
            pointer_path = os.path.join(registry.model_dir, 'production_pointer.json')
            if os.path.exists(pointer_path):
                with open(pointer_path, 'r') as f:
                    pointer = json.load(f)
                status['model_version'] = pointer.get('version')
        except Exception as e:
            logger.warning(f"Could not read model registry: {e}")

    return status

def get_models_list():
    if not HAS_MODEL_REGISTRY:
        raise HTTPException(status_code=501, detail="Model registry not available")
    registry = ModelRegistry()
    models = []
    model_dir = registry.model_dir
    for fname in os.listdir(model_dir):
        if fname.endswith('.pkl') and '_scaler' not in fname and 'production_pointer' not in fname:
            version = fname.replace('.pkl', '')
            meta_path = os.path.join(model_dir, f'{version}_meta.json')
            if os.path.exists(meta_path):
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                models.append({
                    'version': version,
                    'created_at': meta.get('created_at'),
                    'status': meta.get('status', 'candidate'),
                    'metrics': meta.get('metrics', {})
                })
            else:
                models.append({'version': version, 'status': 'unknown'})
    pointer_path = os.path.join(model_dir, 'production_pointer.json')
    if os.path.exists(pointer_path):
        with open(pointer_path, 'r') as f:
            pointer = json.load(f)
        prod_version = pointer.get('version')
        for m in models:
            if m['version'] == prod_version:
                m['status'] = 'production'
                break
    models.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return models

def promote_model(version: str):
    if not HAS_MODEL_REGISTRY:
        raise HTTPException(status_code=501, detail="Model registry not available")
    registry = ModelRegistry()
    model_path = os.path.join(registry.model_dir, f'{version}.pkl')
    scaler_path = os.path.join(registry.model_dir, f'{version}_scaler.pkl')
    meta_path = os.path.join(registry.model_dir, f'{version}_meta.json')
    if not os.path.exists(model_path):
        raise HTTPException(status_code=404, detail=f"Model version {version} not found")
    if os.path.exists(meta_path):
        with open(meta_path, 'r') as f:
            meta = json.load(f)
        meta['status'] = 'production'
        meta['promoted_at'] = datetime.now().isoformat()
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2)
    pointer = {'version': version, 'path': model_path}
    with open(os.path.join(registry.model_dir, 'production_pointer.json'), 'w') as f:
        json.dump(pointer, f, indent=2)
    try:
        import shutil
        shutil.copy(model_path, 'model.pkl')
        shutil.copy(scaler_path, 'scaler.pkl')
    except Exception as e:
        logger.warning(f"Could not create model.pkl symlink: {e}")
    logger.info(f"Promoted model {version} to production")
    return {"status": "success", "version": version}

def get_learning_insights():
    if not HAS_INSIGHTS:
        raise HTTPException(status_code=501, detail="Insights module not available")
    report_path = 'training_report.joblib'
    if not os.path.exists(report_path):
        raise HTTPException(status_code=404, detail="No training report found")
    # Return placeholder insights
    return {
        "insights": [
            {"insight": "Bullish market regimes show higher T+5 success rates", "sample_size": 124, "confidence": "high", "active": True},
            {"insight": "RSI between 50-65 performs best for BUY signals", "sample_size": 87, "confidence": "medium", "active": True},
            {"insight": "Volume > 1.5x average improves win rate by 12%", "sample_size": 65, "confidence": "high", "active": True}
        ],
        "last_updated": datetime.now().isoformat()
    }

def get_summary_metrics():
    if not HAS_DB:
        raise HTTPException(status_code=501, detail="Database not available")
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
                "metrics": metrics
            }
        }
    except Exception as e:
        logger.error(f"Error getting summary metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request, "service_url": SERVICE_URL})

@app.get("/api/status")
async def status():
    return JSONResponse(content=get_training_status())

@app.post("/api/train")
async def trigger_training(background_tasks: BackgroundTasks):
    lock_file = 'training.lock'
    if os.path.exists(lock_file):
        raise HTTPException(status_code=409, detail="Training already in progress")
    with open(lock_file, 'w') as f:
        f.write(str(os.getpid()))

    def run_training():
        try:
            from train import train_model
            # Pass session and model store path
            train_model(SessionLocal(), os.environ.get('MODEL_STORE_PATH', './model-store'))
        except Exception as e:
            logger.error(f"Training failed: {e}")
        finally:
            if os.path.exists(lock_file):
                os.remove(lock_file)

    background_tasks.add_task(run_training)
    return JSONResponse(content={"status": "Training started successfully", "service_url": SERVICE_URL}, status_code=202)

@app.get("/api/report")
async def get_report():
    report_path = 'training_report.joblib'
    if not os.path.exists(report_path):
        raise HTTPException(status_code=404, detail="No report found")
    try:
        report = joblib.load(report_path)
        import numpy as np
        def convert(o):
            if isinstance(o, (np.integer, np.floating)):
                return float(o)
            if isinstance(o, np.ndarray):
                return o.tolist()
            return o
        return JSONResponse(content={k: convert(v) if not isinstance(v, dict) else {kk: convert(vv) for kk, vv in v.items()} for k, v in report.items()})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/models")
async def list_models():
    try:
        models = get_models_list()
        return JSONResponse(content={"models": models})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing models: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/models/promote/{version}")
async def promote_candidate(version: str):
    try:
        result = promote_model(version)
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error promoting model: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/insights")
async def get_insights():
    try:
        insights = get_learning_insights()
        return JSONResponse(content=insights)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating insights: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/metrics/summary")
async def summary():
    try:
        summary_data = get_summary_metrics()
        return JSONResponse(content=summary_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting summary metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ----------------------------------------------------------------------
# Run with uvicorn (for local development)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get('PORT', 5001))
    uvicorn.run(app, host="0.0.0.0", port=port)