

def load_dynamic_training_universe():
    base = load_dynamic_training_universe().copy()
    extra_files=[
        '../news-intelligence-service/trending_symbols.txt',
        '../event-tracker-service/event_symbols.txt',
        'manual_symbols.txt'
    ]
    symbols=set(base)
    import os
    for f in extra_files:
        if os.path.exists(f):
            with open(f) as fh:
                for line in fh:
                    s=line.strip().upper()
                    if s:
                        symbols.add(s)
    return sorted(symbols)

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
import logging
import time

import pandas as pd
import yfinance as yf
from xgboost import XGBClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, roc_auc_score
import joblib

from features import compute_feature_frame, FEATURE_COLUMNS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prediction-train")

# Broader universe than the live watchlist so the model sees more market
# regimes and sectors. Add/remove freely — more symbols = better generalization.
TRAINING_UNIVERSE = [
    "TCS", "INFY", "HDFCBANK", "ICICIBANK", "RELIANCE", "HCLTECH", "WIPRO",
    "COFORGE", "ANGELONE", "ADANIPOWER", "BEL", "HAL", "TATAMOTORS", "SBIN",
    "AXISBANK", "KOTAKBANK", "LT", "MARUTI", "SUNPHARMA", "TITAN", "ITC",
    "BAJFINANCE", "ASIANPAINT", "NESTLEIND", "ULTRACEMCO",
]

LOOKAHEAD_DAYS = 10
TARGET_GAIN_PCT = 5.0


def build_dataset() -> pd.DataFrame:
    rows = []
    for symbol in TRAINING_UNIVERSE:
        sym = f"{symbol}.NS"
        try:
            df = yf.Ticker(sym).history(period="5y", interval="1d")
        except Exception as e:
            logger.warning("Skipping %s: %s", sym, e)
            continue

        if df.empty or len(df) < 250:
            logger.warning("Not enough history for %s, skipping", sym)
            continue

        feat_df = compute_feature_frame(df)
        closes = feat_df["Close"].values

        for i in range(200, len(feat_df) - LOOKAHEAD_DAYS):
            row = feat_df.iloc[i]
            if row[FEATURE_COLUMNS].isna().any():
                continue

            future_close = closes[i + LOOKAHEAD_DAYS]
            current_close = closes[i]
            gain_pct = (future_close - current_close) / current_close * 100
            label = 1 if gain_pct >= TARGET_GAIN_PCT else 0

            record = {col: row[col] for col in FEATURE_COLUMNS}
            record["label"] = label
            record["symbol"] = symbol
            rows.append(record)

        time.sleep(0.3)  # be polite to the free data source
        logger.info("Processed %s — %d rows so far", symbol, len(rows))

    return pd.DataFrame(rows)


def main():
    logger.info("Building training dataset from %d symbols (5y daily history)...", len(TRAINING_UNIVERSE))
    dataset = build_dataset()
    logger.info("Dataset built: %d rows, positive rate %.1f%%", len(dataset), dataset["label"].mean() * 100)

    if len(dataset) < 500:
        logger.error("Not enough data to train a reliable model (%d rows). Check network access to Yahoo Finance.", len(dataset))
        return

    X = dataset[FEATURE_COLUMNS]
    y = dataset["label"]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

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
    logger.info("\n%s", classification_report(y_test, preds))
    logger.info("ROC-AUC: %.3f", roc_auc_score(y_test, probs))

    joblib.dump(model, "model.pkl")
    logger.info("Model saved to model.pkl")


if __name__ == "__main__":
    main()
