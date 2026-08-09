"""
Training-service FastAPI application.
Serves the training dashboard and exposes endpoints to trigger and monitor training.
"""
import os
import subprocess
import logging
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
import joblib
import json

app = FastAPI(title="Training Intelligence", version="1.0")

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Templates (ensure you have a 'templates' folder)
templates = Jinja2Templates(directory="templates")

# Service URL – replace with your actual Render URL
SERVICE_URL = "https://training-service-5e9v.onrender.com"

# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def get_training_status():
    """Return a dict with current training status."""
    report_path = 'training_report.joblib'
    model_path = 'model.pkl'
    status = {
        'service_url': SERVICE_URL,
        'production_model_exists': os.path.exists(model_path),
        'last_training': None,
        'dataset_size': 0,
        'num_symbols': 0,
        'metrics': {},
        'fold_details': []
    }
    if os.path.exists(report_path):
        try:
            report = joblib.load(report_path)
            status['last_training'] = report.get('timestamp')
            status['dataset_size'] = report.get('dataset_size', 0)
            status['num_symbols'] = report.get('num_symbols', 0)
            status['metrics'] = report.get('walk_forward_metrics', {})
            status['fold_details'] = report.get('fold_details', [])
        except Exception as e:
            logger.error(f"Error loading report: {e}")
    return status

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

# ----------------------------------------------------------------------
# Run with uvicorn (for local development)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get('PORT', 5001))
    uvicorn.run(app, host="0.0.0.0", port=port)