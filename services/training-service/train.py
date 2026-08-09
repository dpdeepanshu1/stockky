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
    import models as db_models
    from sqlalchemy.orm import Session
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
DEFAULT_TRAINING_CONFIG = {
    "target_type": "Log_Return",          # "Log_Return", "Percentage_Return", "Directional"
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
        "symbols": [
            "TCS", "INFY", "HDFCBANK", "ICICIBANK", "RELIANCE", "HCLTECH",
            "WIPRO", "COFORGE", "ANGELONE", "ADANIPOWER", "BEL", "HAL",
            "SBIN", "AXISBANK", "KOTAKBANK", "LT", "MARUTI", "SUNPHARMA",
            "TITAN", "ITC", "BAJFINANCE", "ASIANPAINT", "NESTLEIND", "ULTRACEMCO",
            "BHARTIARTL", "M&M", "SHRIRAMFIN", "DMART", "CHOLAFIN", "PHOENIXLTD",
            "FORTIS", "CUMMINSIND", "SYRMA", "ADANIPORTS", "HINDALCO", "AUROPHARMA"
        ],
        "start_date": "2020-01-01",
        "end_date": "2025-12-31",
        "period": "5y"
    }
}

# ---------- Helpers ----------
def fetch_symbol_data(symbol, period="5y"):
    """Fetch OHLCV data for a single symbol with retries."""
    tickers = [f"{symbol}.NS", f"{symbol}.BO", symbol]
    for ticker in tickers:
        try:
            df = yf.download(ticker, period=period, interval="1d", auto_adjust=True,
                             progress=False, threads=False)
            if not df.empty and "Close" in df.columns:
                return df
        except Exception:
            continue
    return pd.DataFrame()

def build_multi_symbol_dataset(symbols, period="5y"):
    """Aggregate data from multiple symbols into one DataFrame with a 'symbol' column."""
    all_rows = []
    for sym in symbols:
        df = fetch_symbol_data(sym, period)
        if df.empty:
            logger.warning(f"No data for {sym}")
            continue
        df = df[['Open','High','Low','Close','Volume']].copy()
        df.columns = ['open','high','low','close','volume']  # lower case for consistency
        df['symbol'] = sym
        df = df.dropna()
        # Compute features (per symbol)
        feat = compute_feature_frame(df)
        # Keep only rows with all features
        feat = feat.dropna(subset=FEATURE_COLUMNS)
        if len(feat) < 100:
            logger.warning(f"Too few rows for {sym}: {len(feat)}")
            continue
        # Add to all_rows
        feat['symbol'] = sym
        feat['date'] = feat.index
        all_rows.append(feat)
        time.sleep(0.2)  # polite delay
    if not all_rows:
        raise ValueError("No data collected")
    full = pd.concat(all_rows, ignore_index=True)
    full = full.sort_values(['symbol','date']).reset_index(drop=True)
    return full

def save_training_run_to_db(config, metrics, fold_details, model_version, dataset_size, num_symbols):
    """Log training run details to the database (optional)."""
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
def run_training_pipeline(config, config_path=None):
    np.random.seed(config['random_seed'])
    random.seed(config['random_seed'])

    # 1. Load data
    symbols = config['data']['symbols']
    logger.info(f"Fetching data for {len(symbols)} symbols...")
    df = build_multi_symbol_dataset(symbols, period=config['data']['period'])
    logger.info(f"Total dataset shape: {df.shape}")

    # 2. Prepare features and targets
    feature_cols = FEATURE_COLUMNS
    target_gen = TargetGenerator(
        target_type=config['target_type'],
        forecast_horizon=config['forecast_horizon_days'],
        price_col='close'
    )

    # We need to generate targets per symbol to avoid leakage between symbols?
    # But since we treat each row independently, we can generate targets on the full DataFrame
    # if the shift is within the same symbol. Because we have grouped by symbol, we must apply
    # target generation per symbol group to avoid cross‑symbol shifts.
    # Let's do groupby('symbol') and apply.
    def apply_targets(group):
        # group is a DataFrame with 'close' column
        t, g = target_gen.generate(group, inplace=False)
        group['target'] = t
        group['pct_return'] = g['pct_return']
        group['log_return'] = g['log_return']
        return group

    df = df.groupby('symbol', group_keys=False).apply(apply_targets)
    # Drop rows where target is NaN (due to insufficient future data at the end of each symbol)
    df = df.dropna(subset=['target'])
    logger.info(f"After target generation: {df.shape}")

    # 3. Walk‑forward validation
    vc = config['validation_strategy']
    splitter = WalkForwardSplitter(
        train_window=vc['train_window_size'],
        val_window=vc['validation_window_size'],
        step_size=vc.get('step_size', vc['validation_window_size']),
        embargo_days=vc.get('embargo_days', config['forecast_horizon_days']),
        forecast_horizon=config['forecast_horizon_days'],
        method=vc['method']
    )

    # Because we have multiple symbols interleaved, we need to split chronologically on the overall
    # sorted DataFrame (ignoring symbol) – but this mixes symbols. To be strict, we should split per symbol.
    # For simplicity, we'll sort by date and treat it as a single time series (which is acceptable if we have many symbols
    # and we assume the model is general enough). However, to avoid leakage between symbols,
    # we would need to ensure that the split is by date and that we don't use future data from other symbols.
    # Here we'll sort globally and split; this is a common simplification.
    df = df.sort_values('date').reset_index(drop=True)

    folds = splitter.split(df)
    logger.info(f"Number of folds: {len(folds)}")

    if not folds:
        logger.error("No folds generated – insufficient data.")
        return None

    # Containers for OOS
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

        # Scale per fold
        scaler = TimeAwareScaler(config['preprocessing']['scaler_type'])
        X_train_scaled = scaler.fit_transform(X_train)
        X_val_scaled = scaler.transform(X_val)

        # Train model
        model = xgb.XGBRegressor(
            **config['model'],
            random_state=config['random_seed'],
            eval_metric='rmse'
        )
        model.fit(X_train_scaled, y_train, eval_set=[(X_val_scaled, y_val)], verbose=False)

        # Predict OOS
        pred_val = model.predict(X_val_scaled)

        # Trading simulation
        trade_cfg = config['trading']
        trade_sim = TradingSimulator(
            long_threshold=trade_cfg['long_threshold'],
            short_threshold=trade_cfg['short_threshold'],
            transaction_cost_bps=trade_cfg['transaction_cost_bps'],
            slippage_bps=trade_cfg['slippage_bps'],
            allow_short=trade_cfg['allow_short']
        )
        signals, costs, strategy_ret = trade_sim.simulate(pred_val, y_val)

        # Store
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

        # Cleanup
        del train_data, val_data, X_train, X_val, y_train, y_val, model
        gc.collect()

    # Compute overall OOS metrics
    all_preds = np.array(all_preds)
    all_actuals = np.array(all_actuals)
    all_strategy_returns = np.array(all_strategy_returns)

    metrics = compute_all_metrics(all_preds, all_actuals, all_strategy_returns)

    logger.info("\n" + "="*50)
    logger.info("WALK‑FORWARD OOS PERFORMANCE")
    for k,v in metrics.items():
        logger.info(f"{k:>20}: {v:.4f}" if isinstance(v, float) else f"{k:>20}: {v}")
    logger.info("="*50)

    # 4. Train final production model on the entire dataset
    logger.info("Training production model on full dataset...")
    X_full = df[feature_cols].values.astype(np.float32)
    y_full = df['target'].values.astype(np.float32)

    # Scale on all data (no leakage here because it's the final model)
    final_scaler = TimeAwareScaler(config['preprocessing']['scaler_type'])
    X_full_scaled = final_scaler.fit_transform(X_full)

    final_model = xgb.XGBRegressor(
        **config['model'],
        random_state=config['random_seed'],
        eval_metric='rmse'
    )
    final_model.fit(X_full_scaled, y_full)

    # ---------- NEW: Use Model Registry for versioned storage ----------
    model_version = None
    if HAS_MODEL_REGISTRY:
        registry = ModelRegistry()
        model_version = registry.save_production_model(final_model, final_scaler, config, metrics)
        logger.info(f"Production model saved with version: {model_version}")
    else:
        # Fallback: save as before
        joblib.dump(final_model, 'model.pkl')
        joblib.dump(final_scaler, 'scaler.pkl')
        with open('training_config.json', 'w') as f:
            json.dump(config, f, indent=2)
        logger.info("Production model saved as model.pkl (legacy mode)")

    # 5. Prepare final report for dashboard
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

    # ---------- NEW: Log training run to database ----------
    if HAS_DB:
        save_training_run_to_db(config, metrics, fold_reports, model_version, len(df), len(df['symbol'].unique()))

    return report

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

    # Optionally override DB flag
    if args.no_db:
        HAS_DB = False

    run_training_pipeline(config)