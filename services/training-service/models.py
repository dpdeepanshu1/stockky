"""
Model registry for production and candidate models.
[reference:28]
"""
import joblib
import os
import json
from datetime import datetime
from typing import Dict, Optional

class ModelRegistry:
    def __init__(self, model_dir: str = './models'):
        self.model_dir = model_dir
        os.makedirs(model_dir, exist_ok=True)

    def save_production_model(self, model, scaler, config: Dict, metrics: Dict) -> str:
        """Save production model with versioning."""
        version = f"v{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        model_path = os.path.join(self.model_dir, f'production_{version}.pkl')
        scaler_path = os.path.join(self.model_dir, f'production_{version}_scaler.pkl')
        meta_path = os.path.join(self.model_dir, f'production_{version}_meta.json')

        joblib.dump(model, model_path)
        joblib.dump(scaler, scaler_path)

        metadata = {
            'version': version,
            'created_at': datetime.now().isoformat(),
            'config': config,
            'metrics': metrics,
            'status': 'production'
        }

        with open(meta_path, 'w') as f:
            json.dump(metadata, f, indent=2)

        # Update production pointer
        with open(os.path.join(self.model_dir, 'production_pointer.json'), 'w') as f:
            json.dump({'version': version, 'path': model_path}, f)

        return version

    def save_candidate_model(self, model, scaler, config: Dict, metrics: Dict) -> str:
        """Save candidate model for validation."""
        version = f"candidate_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        model_path = os.path.join(self.model_dir, f'{version}.pkl')
        scaler_path = os.path.join(self.model_dir, f'{version}_scaler.pkl')
        meta_path = os.path.join(self.model_dir, f'{version}_meta.json')

        joblib.dump(model, model_path)
        joblib.dump(scaler, scaler_path)

        metadata = {
            'version': version,
            'created_at': datetime.now().isoformat(),
            'config': config,
            'metrics': metrics,
            'status': 'candidate'
        }

        with open(meta_path, 'w') as f:
            json.dump(metadata, f, indent=2)

        return version

    def get_production_model(self):
        """Load the current production model."""
        pointer_path = os.path.join(self.model_dir, 'production_pointer.json')
        if not os.path.exists(pointer_path):
            return None, None

        with open(pointer_path, 'r') as f:
            pointer = json.load(f)

        model_path = pointer.get('path')
        if not model_path or not os.path.exists(model_path):
            return None, None

        model = joblib.load(model_path)
        scaler_path = model_path.replace('.pkl', '_scaler.pkl')
        scaler = joblib.load(scaler_path) if os.path.exists(scaler_path) else None

        return model, scaler