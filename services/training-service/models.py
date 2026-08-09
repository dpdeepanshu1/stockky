"""
SQLAlchemy models for the training service.

Tables:
- PredictionSnapshot: stores the feature snapshot at prediction time (immutable).
- PredictionOutcome: stores T+1 and T+5 evaluation results.
- TrainingRun: tracks each training pipeline run (for auditing and metrics).
"""
from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime, JSON, Text, create_engine
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# ---------- Base ----------
Base = declarative_base()


# ---------- Models ----------
class PredictionSnapshot(Base):
    """Immutable snapshot of a prediction at the time it was made."""
    __tablename__ = "prediction_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_id = Column(String(50), unique=True, nullable=False, index=True)  # e.g., STK-20260810-001
    symbol = Column(String(20), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)  # when prediction was made
    price = Column(Float, nullable=False)                     # entry price
    decision = Column(String(20), nullable=False)             # BUY, PREPARE_TO_BUY, etc.
    confidence = Column(String(20))                           # High/Medium/Low
    combined_score = Column(Float)
    technical_score = Column(Float)
    fundamental_score = Column(Float)
    news_score = Column(Float, nullable=True)
    prediction_score = Column(Float, nullable=True)
    market_score = Column(Float)
    market_sentiment_adjustment = Column(Float)
    training_score = Column(Float)
    event_risk = Column(Boolean, default=False)
    entry_range_low = Column(Float, nullable=True)
    entry_range_high = Column(Float, nullable=True)
    target = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    holding_period = Column(String(50), nullable=True)
    support = Column(Float, nullable=True)
    resistance = Column(Float, nullable=True)
    sector = Column(String(50), nullable=True)
    valuation = Column(Text, nullable=True)

    # Market sentiment at prediction time
    market_mood = Column(String(20), nullable=True)           # BULLISH, BEARISH, NEUTRAL
    market_score = Column(Float, nullable=True)
    nifty_change_pct = Column(Float, nullable=True)
    sensex_change_pct = Column(Float, nullable=True)

    # Technical features (snapshot of key indicators)
    rsi = Column(Float, nullable=True)
    macd = Column(String(20), nullable=True)
    ema = Column(String(20), nullable=True)
    volume_ratio = Column(Float, nullable=True)

    # Fundamental features (snapshot)
    debt_to_equity = Column(Float, nullable=True)
    roe = Column(Float, nullable=True)
    roce = Column(Float, nullable=True)

    # Additional feature snapshot as JSON (for flexibility)
    feature_snapshot = Column(JSON, nullable=True)

    # Metadata
    model_version = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Outcome flags (updated after evaluation)
    t1_success = Column(Integer, default=0)   # 0 = pending, 1 = success, 2 = failed
    t5_success = Column(Integer, default=0)
    overall_success = Column(Integer, default=0)


class PredictionOutcome(Base):
    """Evaluation outcomes for a prediction (T+1, T+5)."""
    __tablename__ = "prediction_outcomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_id = Column(String(50), nullable=False, index=True)
    evaluation_period = Column(String(10), nullable=False, index=True)  # 'T+1' or 'T+5'
    evaluation_date = Column(DateTime, nullable=False)

    # Price data for the evaluation day
    open_price = Column(Float, nullable=True)
    high_price = Column(Float, nullable=True)
    low_price = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)

    # Metrics
    max_favorable_excursion = Column(Float, nullable=True)   # percentage
    max_adverse_excursion = Column(Float, nullable=True)    # percentage
    return_pct = Column(Float, nullable=True)

    # Flags
    entry_reached = Column(Integer, default=0)
    target_reached = Column(Integer, default=0)
    stop_loss_reached = Column(Integer, default=0)
    direction_correct = Column(Integer, default=0)
    success = Column(Integer, default=0)   # 1 = success, 0 = failure

    # Additional information
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TrainingRun(Base):
    """Tracks each training pipeline run with configuration and performance."""
    __tablename__ = "training_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_timestamp = Column(DateTime, nullable=False, index=True)
    config = Column(JSON, nullable=False)                     # full config JSON
    dataset_size = Column(Integer)
    num_symbols = Column(Integer)
    model_version = Column(String(50), nullable=True)
    walk_forward_metrics = Column(JSON, nullable=True)        # metrics from evaluation
    fold_details = Column(JSON, nullable=True)                # list of fold info
    created_at = Column(DateTime, default=datetime.utcnow)


# ---------- Database setup helpers ----------
def get_engine(database_url="sqlite:///./training.db"):
    """Return a SQLAlchemy engine."""
    return create_engine(database_url, echo=False)

def create_tables(engine):
    """Create all tables if they don't exist."""
    Base.metadata.create_all(engine)

def get_session(engine):
    """Return a new session."""
    Session = sessionmaker(bind=engine)
    return Session()