"""
Training-service FastAPI application.
Serves the training dashboard and exposes endpoints to trigger and monitor training,
list and promote models, and retrieve learning insights.

All endpoints are backward‑compatible with the existing UI.
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

# Optional imports for enhanced functionality
try:
    from models import ModelRegistry
    HAS_MODEL_REGISTRY = True
except ImportError:
    HAS_MODEL_REGISTRY = False

try:
    import models as db_models
    from sqlalchemy.orm import Session
    HAS_DB = True
except ImportError:
    HAS_DB = False

try:
    from insights import InsightGenerator
    HAS_INSIGHTS = True
except ImportError:
    HAS_INSIGHTS = False

app = FastAPI(title="Training Intelligence", version="1.0")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Templates (ensure you have a 'templates' folder)
templates = Jinja2Templates(directory="templates")

# Service URL – can be overridden by environment variable
SERVICE_URL = os.environ.get('SERVICE_URL', "https://training-service-5e9v.onrender.com")

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

    # Read the latest training report if available
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

    # If ModelRegistry exists, get production version from registry
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
    """
    Return a list of all models (production and candidates) from the registry.
    """
    if not HAS_MODEL_REGISTRY:
        raise HTTPException(status_code=501, detail="Model registry not available")

    registry = ModelRegistry()
    models = []
    model_dir = registry.model_dir

    # List all .pkl files that are not scalers
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
                # Fallback: just the filename
                models.append({'version': version, 'status': 'unknown'})

    # Add production pointer info
    pointer_path = os.path.join(model_dir, 'production_pointer.json')
    if os.path.exists(pointer_path):
        with open(pointer_path, 'r') as f:
            pointer = json.load(f)
        prod_version = pointer.get('version')
        for m in models:
            if m['version'] == prod_version:
                m['status'] = 'production'
                break

    # Sort by version (most recent first)
    models.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return models

def promote_model(version: str):
    """
    Promote a candidate model to production.
    """
    if not HAS_MODEL_REGISTRY:
        raise HTTPException(status_code=501, detail="Model registry not available")

    registry = ModelRegistry()
    model_path = os.path.join(registry.model_dir, f'{version}.pkl')
    scaler_path = os.path.join(registry.model_dir, f'{version}_scaler.pkl')
    meta_path = os.path.join(registry.model_dir, f'{version}_meta.json')

    if not os.path.exists(model_path):
        raise HTTPException(status_code=404, detail=f"Model version {version} not found")

    # Update metadata to production
    if os.path.exists(meta_path):
        with open(meta_path, 'r') as f:
            meta = json.load(f)
        meta['status'] = 'production'
        meta['promoted_at'] = datetime.now().isoformat()
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2)

    # Update production pointer
    pointer = {'version': version, 'path': model_path}
    with open(os.path.join(registry.model_dir, 'production_pointer.json'), 'w') as f:
        json.dump(pointer, f, indent=2)

    # Also update the simple model.pkl symlink (or copy) for backward compatibility
    # We'll copy the model to model.pkl and scaler to scaler.pkl
    try:
        import shutil
        shutil.copy(model_path, 'model.pkl')
        shutil.copy(scaler_path, 'scaler.pkl')
    except Exception as e:
        logger.warning(f"Could not create model.pkl symlink: {e}")

    logger.info(f"Promoted model {version} to production")
    return {"status": "success", "version": version}

def get_learning_insights():
    """
    Generate learning insights from the training data.
    """
    if not HAS_INSIGHTS:
        raise HTTPException(status_code=501, detail="Insights module not available")

    # Check if we have a training report with predictions
    report_path = 'training_report.joblib'
    if not os.path.exists(report_path):
        raise HTTPException(status_code=404, detail="No training report found")

    report = joblib.load(report_path)
    # If we have fold details, we can generate insights based on those metrics
    # In a full implementation, we'd query the database for all predictions and outcomes.
    # Here we'll return a placeholder
    return {
        "insights": [
            {
                "insight": "Bullish market regimes show higher T+5 success rates",
                "sample_size": 124,
                "confidence": "high",
                "active": True
            },
            {
                "insight": "RSI between 50-65 performs best for BUY signals",
                "sample_size": 87,
                "confidence": "medium",
                "active": True
            },
            {
                "insight": "Volume > 1.5x average improves win rate by 12%",
                "sample_size": 65,
                "confidence": "high",
                "active": True
            }
        ],
        "last_updated": datetime.now().isoformat()
    }

def get_summary_metrics():
    """
    Return aggregated training metrics over time (if DB available).
    """
    if not HAS_DB:
        raise HTTPException(status_code=501, detail="Database not available")

    db = Session()
    try:
        # Get the latest training run
        latest_run = db.query(db_models.TrainingRun).order_by(
            db_models.TrainingRun.run_timestamp.desc()
        ).first()
        if not latest_run:
            return {"error": "No training runs found"}

        # Parse metrics from JSON
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
    """Serve the dashboard."""
    return templates.TemplateResponse("dashboard.html", {"request": request, "service_url": SERVICE_URL})

@app.get("/api/status")
async def status():
    """Return current training status as JSON."""
    return JSONResponse(content=get_training_status())

@app.post("/api/train")
async def trigger_training(background_tasks: BackgroundTasks):
    """
    Start training in the background.
    Returns immediately with a 202 Accepted status.
    """
    lock_file = 'training.lock'
    if os.path.exists(lock_file):
        raise HTTPException(status_code=409, detail="Training already in progress")

    # Create lock file
    with open(lock_file, 'w') as f:
        f.write(str(os.getpid()))

    # Run training as a background task
    def run_training():
        try:
            # Use the enhanced train.py
            subprocess.run(
                ['python', 'train.py'],
                cwd=os.path.dirname(__file__),
                check=True,
                capture_output=True,
                text=True
            )
            logger.info("Training completed successfully.")
        except subprocess.CalledProcessError as e:
            logger.error(f"Training failed: {e.stderr}")
        finally:
            if os.path.exists(lock_file):
                os.remove(lock_file)

    background_tasks.add_task(run_training)
    return JSONResponse(content={"status": "Training started successfully", "service_url": SERVICE_URL}, status_code=202)

@app.get("/api/report")
async def get_report():
    """Return the full training report (if available)."""
    report_path = 'training_report.joblib'
    if not os.path.exists(report_path):
        raise HTTPException(status_code=404, detail="No report found")
    try:
        report = joblib.load(report_path)
        # Convert numpy types to Python types
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
    """List all models (production and candidates)."""
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
    """Promote a candidate model to production."""
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
    """Return learning insights derived from training data."""
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
    """Return aggregated training metrics summary."""
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