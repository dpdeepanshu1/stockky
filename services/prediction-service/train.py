"""
Training script for the Prediction Service.

Run this manually (or on a weekly cron) once you have Docker Compose up:

    docker compose run --rm prediction-service python train.py

It builds a labeled dataset directly from Yahoo Finance historical candles
(no need to wait weeks accumulating your own scan history — we can label
the past immediately because we already know what happened next):

  Label = 1 if the close price ~10 trading days after date D is at least
            5% higher than the close on date D (matches the product spec's
            "~5% gain within one month" framing, using 10 trading days ≈ 2
            weeks as a slightly tighter, more actionable window)
  Label = 0 otherwise

Features = the same technical-indicator snapshot the live service computes
           at inference time (see features.py) — this is what keeps
           train/serve consistent.

Saves the trained model to model.pkl, which main.py loads at startup.
"""

import os
import logging
import time
import random
import signal
import sys
import pandas as pd
import yfinance as yf
from xgboost import XGBClassifier
from sklearn.metrics import classification_report, roc_auc_score
import joblib

from features import compute_feature_frame, FEATURE_COLUMNS

# ----------------------------------------------------------------------
# Logging setup
# ----------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prediction-train")

# Silence yfinance's own ERROR logs (they are noisy and we handle them)
logging.getLogger("yfinance").setLevel(logging.WARNING)

# ----------------------------------------------------------------------
# Graceful exit on Ctrl+C
# ----------------------------------------------------------------------
def signal_handler(sig, frame):
    logger.info("\nTraining interrupted by user. Exiting gracefully...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# ----------------------------------------------------------------------
# Base universe + dynamic extras
# ----------------------------------------------------------------------
BASE_TRAINING_UNIVERSE = [
    # === Existing (retained) ===
    "TCS", "INFY", "HDFCBANK", "ICICIBANK", "RELIANCE", "HCLTECH",
    "WIPRO", "COFORGE", "ANGELONE", "ADANIPOWER", "BEL", "HAL",
    "TMPV", "TMCV",  # Tata Motors demerged entities
    "SBIN", "AXISBANK", "KOTAKBANK", "LT",
    "MARUTI", "SUNPHARMA", "TITAN", "ITC",
    "BAJFINANCE", "ASIANPAINT", "NESTLEIND", "ULTRACEMCO",

    # === New: Top broker picks (August 2026) ===
    "BHARTIARTL", "M&M", "SHRIRAMFIN",
    "INDIGO",       # Corrected from INTERGLOBE
    "VARUNBEV", "DMART", "CHOLAFIN", "PHOENIXLTD",
    "FORTIS", "CUMMINSIND", "SYRMA", "ADANIPORTS", "HINDALCO",
    "AUROPHARMA", "NAVINFLUOR",
    # "POLICYBZ",   # Removed – not available on Yahoo Finance
    "NEULANDLAB", "BIOCON",
    "BAJAJ-AUTO", "PAYTM",
    "MPHASIS", "RICOAUTO",
    # NOTE: INDEGENE removed (no data)
]


def load_dynamic_training_universe():
    """Combine base universe with external files (updated by other services)."""
    symbols = set(BASE_TRAINING_UNIVERSE)

    extra_files = [
        "../news-intelligence-service/trending_symbols.txt",
        "../event-tracker-service/event_symbols.txt",
        "manual_symbols.txt",
    ]

    for file in extra_files:
        if os.path.exists(file):
            with open(file, "r", encoding="utf-8") as f:
                for line in f:
                    symbol = line.strip().upper()
                    if symbol:
                        symbols.add(symbol)
                        logger.debug("Added %s from %s", symbol, file)

    return sorted(symbols)


TRAINING_UNIVERSE = load_dynamic_training_universe()

logger.info("=" * 80)
logger.info("Training started")
logger.info("Total symbols : %d", len(TRAINING_UNIVERSE))
logger.info("=" * 80)

LOOKAHEAD_DAYS = 10
TARGET_GAIN_PCT = 5.0


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Fix MultiIndex columns and ensure 1D Series."""
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)  # keep price level
    for col in df.columns:
        if isinstance(df[col], pd.DataFrame):
            df[col] = df[col].squeeze()
    return df


def fetch_with_retry(symbol: str, max_retries: int = 3) -> pd.DataFrame:
    """
    Download data with exponential backoff.
    Returns empty DataFrame if all retries fail.
    """
    tickers = [f"{symbol}.NS", f"{symbol}.BO", symbol]
    for ticker in tickers:
        for attempt in range(max_retries):
            try:
                df = yf.download(
                    ticker,
                    period="5y",
                    interval="1d",
                    auto_adjust=True,
                    progress=False,
                    threads=False,
                )
                if not df.empty and "Close" in df.columns:
                    logger.info("Fetched %s from yfinance (%s)", symbol, ticker)
                    return df
                else:
                    # Empty data – try next ticker or retry
                    break
            except Exception as e:
                # If it's a rate‑limit error, wait longer
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "Download failed for %s (attempt %d/%d): %s. Retrying in %.1fs",
                    ticker, attempt+1, max_retries, str(e)[:50], wait
                )
                time.sleep(wait)
        # If we get here, the ticker didn't work; try next ticker
    logger.warning("All sources failed for %s", symbol)
    return pd.DataFrame()


def build_dataset() -> pd.DataFrame:
    rows = []
    failed_symbols = []

    for symbol in TRAINING_UNIVERSE:
        df = fetch_with_retry(symbol)
        if df.empty:
            failed_symbols.append(symbol)
            continue

        if "Close" not in df.columns or len(df) < 250:
            logger.warning("%s: insufficient data (<250 days)", symbol)
            continue

        df = _normalize_df(df)

        try:
            feat_df = compute_feature_frame(df)
        except Exception as e:
            logger.warning("Feature generation failed for %s: %s", symbol, str(e)[:100])
            continue

        closes = feat_df["Close"].values
        for i in range(200, len(feat_df) - LOOKAHEAD_DAYS):
            row = feat_df.iloc[i]
            if row[FEATURE_COLUMNS].isna().any():
                continue
            future_close = closes[i + LOOKAHEAD_DAYS]
            gain = (future_close - closes[i]) / closes[i] * 100
            label = 1 if gain >= TARGET_GAIN_PCT else 0
            record = {col: row[col] for col in FEATURE_COLUMNS}
            record["label"] = label
            record["symbol"] = symbol
            record["date"] = feat_df.index[i]
            rows.append(record)

        # Polite delay between symbols (with slight jitter)
        time.sleep(0.3 + random.uniform(0, 0.2))
        logger.info("Processed %s — %d rows so far", symbol, len(rows))

    if failed_symbols:
        logger.warning("Skipped %d symbols due to download failures: %s",
                       len(failed_symbols), ", ".join(failed_symbols[:5]))
    return pd.DataFrame(rows)


def main():
    logger.info("Building dataset from %d symbols...", len(TRAINING_UNIVERSE))
    dataset = build_dataset()

    if dataset.empty:
        logger.error("No data retrieved. Check network / Yahoo Finance.")
        return

    logger.info("Dataset: %d rows, positive rate %.1f%%",
                len(dataset), dataset["label"].mean() * 100)

    if len(dataset) < 500:
        logger.error("Too few rows (%d).", len(dataset))
        return

    X = dataset[FEATURE_COLUMNS]
    y = dataset["label"]

    # Random splitting is the wrong tool for time-series data like this:
    # a stock's indicators on day N and day N+1 are nearly identical, so a
    # random split routinely puts near-duplicate rows on both sides,
    # letting the model "recognize" test examples it effectively already
    # saw in training. That inflates the reported accuracy without proving
    # the model generalizes to genuinely unseen future periods — which is
    # the only way it'll ever actually be used in production.
    #
    # Instead: pick one calendar cutoff shared across every stock. Train on
    # everything before it, test only on what comes after — mirroring
    # exactly how the trained model gets used later (trained once on
    # history, then scored against dates it has never seen).
    cutoff_date = dataset["date"].quantile(0.8, interpolation="lower")
    train_mask = dataset["date"] < cutoff_date
    test_mask = ~train_mask

    X_train, y_train = X[train_mask], y[train_mask]
    X_test, y_test = X[test_mask], y[test_mask]

    logger.info("Time-based split — cutoff date: %s", cutoff_date.date())
    logger.info(
        "Train: %d rows (%s to %s), positive rate %.1f%%",
        len(X_train),
        dataset.loc[train_mask, "date"].min().date(),
        dataset.loc[train_mask, "date"].max().date(),
        y_train.mean() * 100,
    )
    logger.info(
        "Test:  %d rows (%s to %s), positive rate %.1f%%",
        len(X_test),
        dataset.loc[test_mask, "date"].min().date(),
        dataset.loc[test_mask, "date"].max().date(),
        y_test.mean() * 100,
    )

    if len(X_test) < 50 or y_test.nunique() < 2:
        logger.error(
            "Test set too small or single-class after the time split (%d rows). "
            "Need a longer training history to get a trustworthy holdout.",
            len(X_test),
        )
        return

    model = XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        eval_metric="logloss",
        random_state=42,
    )
    model.fit(X_train, y_train)

    preds = model.predict(X_test)
    probs = model.predict_proba(X_test)[:, 1]

    logger.info("\nOut-of-time test performance (dates the model never trained on):")
    logger.info("\n%s", classification_report(y_test, preds))
    logger.info("ROC-AUC: %.3f", roc_auc_score(y_test, probs))

    joblib.dump(model, "model.pkl")
    logger.info("Model saved to model.pkl")

    logger.info("=" * 80)
    logger.info("Training completed successfully")
    logger.info("Rows : %d", len(dataset))
    logger.info("Model saved : model.pkl")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()