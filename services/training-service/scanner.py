"""
Training Scanner: Finds stocks that resemble historically successful setups.
"""
import os
import json
import logging
from typing import Dict, Any, Optional, List
import joblib
import pandas as pd
import numpy as np
from sqlalchemy.orm import Session

# ✅ Absolute import
import models as db_models

logger = logging.getLogger("training-service.scanner")

class TrainingScanner:
    def __init__(self, db_session_maker, model_store_path: str):
        self.db_session_maker = db_session_maker
        self.model_store_path = model_store_path
        self.model = None
        self.features = []
        self._load_model()

    def _load_model(self):
        """Load the production model if available."""
        prod_meta_path = os.path.join(self.model_store_path, "production_model_metadata.json")
        if not os.path.exists(prod_meta_path):
            logger.warning("No production model found")
            return
        
        with open(prod_meta_path, 'r') as f:
            metadata = json.load(f)
            version = metadata.get('version')
            self.features = metadata.get('features', [])
            
            model_path = os.path.join(self.model_store_path, f"model_{version}.pkl")
            if os.path.exists(model_path):
                self.model = joblib.load(model_path)
                logger.info(f"Loaded production model {version}")
            else:
                logger.warning(f"Model file {model_path} not found")

    def score_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Score a symbol based on historical successful setups."""
        if self.model is None:
            return None

        # Fetch the latest prediction snapshot for this symbol
        db = self.db_session_maker()
        try:
            pred = db.query(db_models.PredictionSnapshot).filter(
                db_models.PredictionSnapshot.symbol == symbol
            ).order_by(db_models.PredictionSnapshot.timestamp.desc()).first()
            
            if not pred:
                return None

            # Build feature vector from the snapshot
            features = pred.features.copy()
            features.update(pred.market_sentiment)
            
            # Ensure all features are present
            X = pd.DataFrame([features])
            X = X.reindex(columns=self.features, fill_value=0)
            X = X.fillna(0)

            # Get prediction probability
            prob = self.model.predict_proba(X)[0, 1] if hasattr(self.model, 'predict_proba') else 0.5
            
            # Compute similarity to historical successful setups
            similarity = self._compute_similarity(features)
            
            return {
                'symbol': symbol,
                'training_score': int(round(prob * 100)),
                't1_success_probability': round(prob * 0.9 + 0.05, 2),  # Placeholder
                't5_success_probability': round(prob * 0.85 + 0.1, 2),
                'historical_similarity': similarity,
                'similar_setups': self._get_similar_setups(features, 3)
            }
        except Exception as e:
            logger.error(f"Error scoring {symbol}: {e}")
            return None
        finally:
            db.close()

    def _compute_similarity(self, features: Dict) -> float:
        """Compute similarity to historical successful setups."""
        # Placeholder - in a real implementation, this would use a similarity metric
        # like cosine similarity between feature vectors
        return round(0.75 + np.random.random() * 0.2, 2)

    def _get_similar_setups(self, features: Dict, limit: int) -> List[Dict]:
        """Get the most similar historical successful setups."""
        # Placeholder
        return [
            {"symbol": "HAL", "similarity": 0.92, "outcome": "success"},
            {"symbol": "BEL", "similarity": 0.88, "outcome": "success"},
            {"symbol": "TCS", "similarity": 0.85, "outcome": "success"}
        ][:limit]