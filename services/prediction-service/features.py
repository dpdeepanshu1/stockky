"""
Shared feature engineering for the Prediction Service.

Used by both `train.py` (building the training set from historical candles)
and `main.py` (computing the same features live at inference time). Keeping
this in one module guarantees train/serve feature parity — the most common
way ML services silently break.
"""
import pandas as pd
import pandas_ta as ta
import numpy as np


FEATURE_COLUMNS = [
    "rsi_14", "macd_hist", "ema20_over_ema50", "ema50_over_ema200",
    "close_over_ema20", "adx_14", "bb_pct", "volume_ratio_20",
    "dist_from_20d_high_pct", "dist_from_20d_low_pct", "atr_pct",
]


def compute_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Given a DataFrame with Open/High/Low/Close/Volume columns (any length,
    but needs >= 200 rows for stable EMA200), return a DataFrame indexed the
    same way with the engineered feature columns added. Rows near the start
    will have NaNs until indicators warm up — callers should drop those."""
    df = df.copy()
    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.ema(length=20, append=True)
    df.ta.ema(length=50, append=True)
    df.ta.ema(length=200, append=True)
    df.ta.adx(length=14, append=True)
    df.ta.atr(length=14, append=True)
    df.ta.bbands(length=20, append=True)

    macdh_col = next((c for c in df.columns if c.startswith("MACDh_")), None)
    bbp_col = next((c for c in df.columns if c.startswith("BBP_")), None)

    df["rsi_14"] = df["RSI_14"]
    df["macd_hist"] = df[macdh_col] if macdh_col else np.nan
    df["ema20_over_ema50"] = df["EMA_20"] / df["EMA_50"]
    df["ema50_over_ema200"] = df["EMA_50"] / df["EMA_200"]
    df["close_over_ema20"] = df["Close"] / df["EMA_20"]
    df["adx_14"] = df[next(c for c in df.columns if c.startswith("ADX_"))]
    df["bb_pct"] = df[bbp_col] if bbp_col else np.nan
    df["volume_ratio_20"] = df["Volume"] / df["Volume"].rolling(20).mean()
    df["dist_from_20d_high_pct"] = (df["High"].rolling(20).max() - df["Close"]) / df["Close"] * 100
    df["dist_from_20d_low_pct"] = (df["Close"] - df["Low"].rolling(20).min()) / df["Close"] * 100
    df["atr_pct"] = df[next(c for c in df.columns if c.startswith("ATRr_"))] / df["Close"] * 100

    return df


def latest_feature_vector(df: pd.DataFrame) -> dict:
    """Feature dict for the most recent row — used at inference time."""
    feat_df = compute_feature_frame(df)
    latest = feat_df.iloc[-1]
    return {col: float(latest[col]) for col in FEATURE_COLUMNS if pd.notna(latest[col])}
