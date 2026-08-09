"""
Training script for the Training Service.
Builds labeled dataset from historical predictions and outcomes,
trains an XGBoost model, and saves it.
"""
import os
import json
import logging
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, roc_auc_score
import xgboost as xgb
import joblib
from sqlalchemy.orm import Session

from . import models as db_models

logger = logging.getLogger("training-service.train")

def build_dataset(db_session: Session):
    """Build a labeled dataset from prediction snapshots and outcomes."""
    # Fetch all predictions that have T+1 outcomes
    predictions = db_session.query(db_models.PredictionSnapshot).all()
    data = []
    
    for pred in predictions:
        # Get T+1 outcome
        outcome = db_session.query(db_models.PredictionOutcome).filter(
            db_models.PredictionOutcome.prediction_id == pred.prediction_id,
            db_models.PredictionOutcome.evaluation_period == 'T+1'
        ).first()
        
        if not outcome:
            continue
        
        # Create feature vector from prediction features + market sentiment
        features = pred.features.copy()
        features.update(pred.market_sentiment)
        
        # Label: 1 if successful (target reached or positive return), else 0
        label = 1 if outcome.success else 0
        
        data.append({
            'features': features,
            'label': label,
            'prediction_id': pred.prediction_id
        })
    
    return pd.DataFrame(data)

def train_model(db_session_maker, model_store_path: str):
    """Train a new XGBoost model."""
    logger.info("Starting training run...")
    
    db = db_session_maker()
    try:
        # Build dataset
        df = build_dataset(db)
        if len(df) < 50:
            logger.warning("Not enough data to train. Need at least 50 samples.")
            return
        
        # Prepare features and labels
        X = pd.json_normalize(df['features'].tolist())
        y = df['label'].values
        
        # Handle missing values
        X = X.fillna(0)
        
        # Train/validation split
        X_train, X_val, y_train, y_val = train_test_split(
            X, y, test_size=0.2, random_state=42, shuffle=False  # Time-series aware would be better
        )
        
        # Train XGBoost
        model = xgb.XGBClassifier(
            n_estimators=100,
            max_depth=6,
            learning_rate=0.1,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42
        )
        model.fit(X_train, y_train)
        
        # Evaluate
        y_pred = model.predict(X_val)
        y_prob = model.predict_proba(X_val)[:, 1]
        
        report = classification_report(y_val, y_pred, output_dict=True)
        auc = roc_auc_score(y_val, y_prob)
        
        metrics = {
            'accuracy': report['accuracy'],
            'precision': report['1']['precision'] if '1' in report else 0,
            'recall': report['1']['recall'] if '1' in report else 0,
            'f1': report['1']['f1-score'] if '1' in report else 0,
            'roc_auc': auc,
            'train_size': len(X_train),
            'val_size': len(X_val)
        }
        
        logger.info(f"Training metrics: {metrics}")
        
        # Save model and metadata
        version = f"v{datetime.now().strftime('%Y%m%d%H%M%S')}"
        model_path = os.path.join(model_store_path, f"model_{version}.pkl")
        joblib.dump(model, model_path)
        
        metadata = {
            'version': version,
            'training_date': datetime.now().isoformat(),
            'features': list(X.columns),
            'metrics': metrics,
            'status': 'candidate'
        }
        
        with open(os.path.join(model_store_path, f"model_{version}_metadata.json"), 'w') as f:
            json.dump(metadata, f, indent=2)
        
        # Update candidate model pointer
        with open(os.path.join(model_store_path, "candidate_model_metadata.json"), 'w') as f:
            json.dump(metadata, f, indent=2)
        
        # Save training run record
        run = db_models.TrainingRun(
            model_version=version,
            dataset_start=datetime.now() - timedelta(days=30),
            dataset_end=datetime.now(),
            features_used=list(X.columns),
            parameters=model.get_params(),
            metrics=metrics,
            status='completed'
        )
        db.add(run)
        db.commit()
        
        logger.info(f"Training completed. Model version: {version}")
        
    except Exception as e:
        logger.error(f"Training failed: {e}")
        db.rollback()
        raise
    finally:
        db.close()