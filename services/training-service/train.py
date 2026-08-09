# services/training-service/train.py
"""
Training script for training‑service.
Uses financial ML best practices: walk‑forward, per‑fold scaling, financial metrics.
Enhanced with model versioning, database logging, and configurable inputs.
"""
import os
import sys
import json
import logging
import argparse
import numpy as np
import pandas as pd
import yfinance as yf
import xgboost as xgb
import joblib
import gc
import time
import random
from datetime import datetime
from typing import List, Dict, Any

# Import our modules
from targets import TargetGenerator
from walk_forward import WalkForwardSplitter
from preprocessing import TimeAwareScaler
from metrics import compute_all_metrics
from trading import TradingSimulator

# Optional imports for enhanced functionality
try:
    from models import ModelRegistry
    HAS_MODEL_REGISTRY = True
except ImportError:
    HAS_MODEL_REGISTRY = False

try:
    from sqlalchemy.orm import Session
    import models as db_models
    HAS_DB = True
except ImportError:
    HAS_DB = False

# If you have a features.py file, import it; otherwise define a minimal feature set.
try:
    from features import compute_feature_frame, FEATURE_COLUMNS
except ImportError:
    # Fallback: define a simple feature set (you can expand)
    FEATURE_COLUMNS = ['sma_10', 'sma_30', 'ema_10', 'rsi', 'volatility', 'volume_sma']
    def compute_feature_frame(df):
        df = df.copy()
        df['sma_10'] = df['Close'].rolling(10).mean()
        df['sma_30'] = df['Close'].rolling(30).mean()
        df['ema_10'] = df['Close'].ewm(span=10, adjust=False).mean()
        df['rsi'] = compute_rsi(df['Close'], 14)
        df['volatility'] = df['Close'].pct_change().rolling(10).std()
        df['volume_sma'] = df['Volume'].rolling(10).mean()
        return df.dropna()
    def compute_rsi(series, period=14):
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("training-service")

# ---------- Configuration ----------
DEFAULT_SYMBOLS = [
    "TCS", "INFY", "HDFCBANK", "ICICIBANK", "RELIANCE", "HCLTECH",
    "WIPRO", "COFORGE", "ANGELONE", "ADANIPOWER", "BEL", "HAL",
    "SBIN", "AXISBANK", "KOTAKBANK", "LT", "MARUTI", "SUNPHARMA",
    "TITAN", "ITC", "BAJFINANCE", "ASIANPAINT", "NESTLEIND", "ULTRACEMCO",
    "BHARTIARTL", "M&M", "SHRIRAMFIN", "DMART", "CHOLAFIN", "PHOENIXLTD",
    "FORTIS", "CUMMINSIND", "SYRMA", "ADANIPORTS", "HINDALCO", "AUROPHARMA"
]

DEFAULT_TRAINING_CONFIG = {
    "target_type": "Log_Return",
    "forecast_horizon_days": 5,
    "validation_strategy": {
        "method": "WalkForward",
        "train_window_size": 252,
        "validation_window_size": 63,
        "step_size": 63,
        "embargo_days": 5
    },
    "preprocessing": {
        "scaler_type": "RobustScaler"
    },
    "model": {
        "max_depth": 4,
        "learning_rate": 0.05,
        "n_estimators": 200,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "reg_alpha": 0.5,
        "reg_lambda": 1.0,
        "tree_method": "hist"
    },
    "trading": {
        "long_threshold": 0.0,
        "short_threshold": 0.0,
        "transaction_cost_bps": 5.0,
        "slippage_bps": 2.0,
        "allow_short": False
    },
    "random_seed": 42,
    "data": {
        "symbols": DEFAULT_SYMBOLS,
        "period": "5y"
    }
}

# ---------- Helpers ----------
def fetch_symbol_data(symbol, period="5y", max_retries=5):
    """Fetch OHLCV data with exponential backoff for rate limits."""
    tickers = [f"{symbol}.NS", f"{symbol}.BO", symbol]
    for ticker in tickers:
        for attempt in range(max_retries):
            try:
                df = yf.download(ticker, period=period, interval="1d", auto_adjust=True,
                                 progress=False, threads=False)
                if not df.empty and "Close" in df.columns:
                    return df
                else:
                    break  # try next ticker
            except Exception as e:
                if "Rate limit" in str(e) or "Too Many Requests" in str(e):
                    wait = (2 ** attempt) * 5 + random.uniform(0, 5)
                    logger.warning(f"Rate limit for {ticker}, retrying in {wait:.1f}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(wait)
                else:
                    logger.warning(f"Error fetching {ticker}: {e}")
                    break
    return pd.DataFrame()

def build_multi_symbol_dataset(symbols, period="5y"):
    """Build dataset with delays between symbols to avoid rate limits."""
    all_rows = []
    total = len(symbols)
    for idx, sym in enumerate(symbols):
        logger.info(f"Fetching {sym} ({idx+1}/{total})...")
        df = fetch_symbol_data(sym, period)
        if df.empty:
            logger.warning(f"No data for {sym}")
            continue
        df = df[['Open','High','Low','Close','Volume']].copy()
        df.columns = ['open','high','low','close','volume']
        df['symbol'] = sym
        df = df.dropna()
        feat = compute_feature_frame(df)
        feat = feat.dropna(subset=FEATURE_COLUMNS)
        if len(feat) < 100:
            logger.warning(f"Too few rows for {sym}: {len(feat)}")
            continue
        feat['symbol'] = sym
        feat['date'] = feat.index
        all_rows.append(feat)
        # Polite delay between symbols
        delay = 1.0 + random.uniform(0, 1.0)
        time.sleep(delay)
    if not all_rows:
        raise ValueError("No data collected")
    full = pd.concat(all_rows, ignore_index=True)
    full = full.sort_values(['symbol','date']).reset_index(drop=True)
    return full

def save_training_run_to_db(config, metrics, fold_details, model_version, dataset_size, num_symbols):
    if not HAS_DB:
        return
    try:
        db = Session()
        run = db_models.TrainingRun(
            run_timestamp=datetime.now(),
            config=json.dumps(config),
            dataset_size=dataset_size,
            num_symbols=num_symbols,
            model_version=model_version,
            walk_forward_metrics=json.dumps(metrics),
            fold_details=json.dumps(fold_details)
        )
        db.add(run)
        db.commit()
        logger.info("Training run logged to database")
    except Exception as e:
        logger.error(f"Failed to log training run to DB: {e}")
    finally:
        db.close()

# ---------- Main Training Pipeline ----------
def run_training_pipeline(config: Dict[str, Any]) -> Dict[str, Any]:
    np.random.seed(config['random_seed'])
    random.seed(config['random_seed'])

    symbols = config['data']['symbols']
    logger.info(f"Fetching data for {len(symbols)} symbols...")
    df = build_multi_symbol_dataset(symbols, period=config['data']['period'])
    logger.info(f"Total dataset shape: {df.shape}")

    feature_cols = FEATURE_COLUMNS
    target_gen = TargetGenerator(
        target_type=config['target_type'],
        forecast_horizon=config['forecast_horizon_days'],
        price_col='close'
    )

    def apply_targets(group):
        t, g = target_gen.generate(group, inplace=False)
        group['target'] = t
        group['pct_return'] = g['pct_return']
        group['log_return'] = g['log_return']
        return group

    df = df.groupby('symbol', group_keys=False).apply(apply_targets)
    df = df.dropna(subset=['target'])
    logger.info(f"After target generation: {df.shape}")

    vc = config['validation_strategy']
    splitter = WalkForwardSplitter(
        train_window=vc['train_window_size'],
        val_window=vc['validation_window_size'],
        step_size=vc.get('step_size', vc['validation_window_size']),
        embargo_days=vc.get('embargo_days', config['forecast_horizon_days']),
        forecast_horizon=config['forecast_horizon_days'],
        method=vc['method']
    )

    df = df.sort_values('date').reset_index(drop=True)
    folds = splitter.split(df)
    logger.info(f"Number of folds: {len(folds)}")
    if not folds:
        logger.error("No folds generated – insufficient data.")
        return None

    all_preds = []
    all_actuals = []
    all_strategy_returns = []
    fold_reports = []

    for i, fold in enumerate(folds):
        logger.info(f"\n--- Fold {i+1}/{len(folds)} ---")
        train_idx = list(range(fold.train_start, fold.train_end + 1))
        val_idx = list(range(fold.val_start, fold.val_end + 1))

        train_data = df.iloc[train_idx].copy()
        val_data = df.iloc[val_idx].copy()

        X_train = train_data[feature_cols].values.astype(np.float32)
        y_train = train_data['target'].values.astype(np.float32)
        X_val = val_data[feature_cols].values.astype(np.float32)
        y_val = val_data['target'].values.astype(np.float32)

        scaler = TimeAwareScaler(config['preprocessing']['scaler_type'])
        X_train_scaled = scaler.fit_transform(X_train)
        X_val_scaled = scaler.transform(X_val)

        model = xgb.XGBRegressor(
            **config['model'],
            random_state=config['random_seed'],
            eval_metric='rmse'
        )
        model.fit(X_train_scaled, y_train, eval_set=[(X_val_scaled, y_val)], verbose=False)

        pred_val = model.predict(X_val_scaled)

        trade_cfg = config['trading']
        trade_sim = TradingSimulator(
            long_threshold=trade_cfg['long_threshold'],
            short_threshold=trade_cfg['short_threshold'],
            transaction_cost_bps=trade_cfg['transaction_cost_bps'],
            slippage_bps=trade_cfg['slippage_bps'],
            allow_short=trade_cfg['allow_short']
        )
        signals, costs, strategy_ret = trade_sim.simulate(pred_val, y_val)

        all_preds.extend(pred_val)
        all_actuals.extend(y_val)
        all_strategy_returns.extend(strategy_ret)

        fold_reports.append({
            'fold': i+1,
            'train_start': df.iloc[fold.train_start]['date'].strftime('%Y-%m-%d'),
            'train_end': df.iloc[fold.train_end]['date'].strftime('%Y-%m-%d'),
            'val_start': df.iloc[fold.val_start]['date'].strftime('%Y-%m-%d'),
            'val_end': df.iloc[fold.val_end]['date'].strftime('%Y-%m-%d'),
            'train_samples': len(train_data),
            'val_samples': len(val_data)
        })

        del train_data, val_data, X_train, X_val, y_train, y_val, model
        gc.collect()

    all_preds = np.array(all_preds)
    all_actuals = np.array(all_actuals)
    all_strategy_returns = np.array(all_strategy_returns)

    metrics = compute_all_metrics(all_preds, all_actuals, all_strategy_returns)

    logger.info("\n" + "="*50)
    logger.info("WALK‑FORWARD OOS PERFORMANCE")
    for k,v in metrics.items():
        logger.info(f"{k:>20}: {v:.4f}" if isinstance(v, float) else f"{k:>20}: {v}")
    logger.info("="*50)

    # Train final production model
    logger.info("Training production model on full dataset...")
    X_full = df[feature_cols].values.astype(np.float32)
    y_full = df['target'].values.astype(np.float32)

    final_scaler = TimeAwareScaler(config['preprocessing']['scaler_type'])
    X_full_scaled = final_scaler.fit_transform(X_full)

    final_model = xgb.XGBRegressor(
        **config['model'],
        random_state=config['random_seed'],
        eval_metric='rmse'
    )
    final_model.fit(X_full_scaled, y_full)

    model_version = None
    if HAS_MODEL_REGISTRY:
        registry = ModelRegistry()
        model_version = registry.save_production_model(final_model, final_scaler, config, metrics)
        logger.info(f"Production model saved with version: {model_version}")
    else:
        joblib.dump(final_model, 'model.pkl')
        joblib.dump(final_scaler, 'scaler.pkl')
        with open('training_config.json', 'w') as f:
            json.dump(config, f, indent=2)
        logger.info("Production model saved as model.pkl (legacy mode)")

    report = {
        'timestamp': datetime.now().isoformat(),
        'dataset_size': len(df),
        'num_symbols': len(df['symbol'].unique()),
        'walk_forward_metrics': metrics,
        'fold_details': fold_reports,
        'production_model_saved': True,
        'model_version': model_version,
        'config': config
    }

    joblib.dump(report, 'training_report.joblib')
    logger.info("Training report saved to training_report.joblib")

    if HAS_DB:
        save_training_run_to_db(config, metrics, fold_reports, model_version, len(df), len(df['symbol'].unique()))

    return report

# ============================================================
# Entry point for FastAPI background task
# ============================================================
def train_model(db_session, model_store_path: str):
    """
    Training entry point called by main.py.
    
    Args:
        db_session: SQLAlchemy session (optional, kept for compatibility)
        model_store_path: Path to store trained models
    """
    logger.info("=" * 60)
    logger.info("TRAINING STARTED (via train_model)")
    logger.info("=" * 60)
    
    if model_store_path:
        os.environ["MODEL_STORE_PATH"] = model_store_path

    config = DEFAULT_TRAINING_CONFIG.copy()
    # Allow override of symbols via environment variable
    env_symbols = os.getenv("TRAINING_SYMBOLS")
    if env_symbols:
        config['data']['symbols'] = [s.strip() for s in env_symbols.split(',') if s.strip()]
        logger.info(f"Using symbols from environment: {config['data']['symbols']}")

    if os.getenv("TRAINING_CONFIG_PATH"):
        try:
            with open(os.getenv("TRAINING_CONFIG_PATH"), 'r') as f:
                config.update(json.load(f))
            logger.info(f"Loaded config from {os.getenv('TRAINING_CONFIG_PATH')}")
        except Exception as e:
            logger.warning(f"Could not load config file: {e}")
    
    try:
        report = run_training_pipeline(config)
        if report:
            logger.info("Training completed successfully")
            logger.info(f"Dataset size: {report.get('dataset_size', 0)}")
            logger.info(f"Model version: {report.get('model_version', 'unknown')}")
        else:
            logger.error("Training pipeline returned no report")
    except Exception as e:
        logger.error(f"Training failed: {e}")
        raise

# ---------- Entry point ----------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run Stockky training pipeline')
    parser.add_argument('--config', type=str, help='Path to training config JSON file')
    parser.add_argument('--no-db', action='store_true', help='Disable database logging')
    args = parser.parse_args()

    config = None
    if args.config and os.path.exists(args.config):
        with open(args.config, 'r') as f:
            config = json.load(f)
        logger.info(f"Loaded config from {args.config}")
    else:
        config = DEFAULT_TRAINING_CONFIG
        logger.info("Using default configuration")

    if args.no_db:
        HAS_DB = False

    run_training_pipeline(config)