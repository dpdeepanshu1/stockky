# services/training-service/models.py
"""
SQLAlchemy models for the training service.

Tables:
- PredictionSnapshot: stores the feature snapshot at prediction time (immutable).
- PredictionOutcome: stores T+1 and T+5 evaluation results.
- TrainingRun: tracks each training pipeline run (for auditing and metrics).
"""
from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime, JSON, Text, create_engine, inspect, text
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
    prediction_id = Column(String(50), unique=True, nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    price = Column(Float, nullable=False)
    decision = Column(String(20), nullable=False)
    confidence = Column(String(20))
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
    market_mood = Column(String(20), nullable=True)
    market_score_extra = Column(Float, nullable=True)  # renamed to avoid conflict
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
    evaluation_period = Column(String(10), nullable=False, index=True)
    evaluation_date = Column(DateTime, nullable=False)

    open_price = Column(Float, nullable=True)
    high_price = Column(Float, nullable=True)
    low_price = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)

    max_favorable_excursion = Column(Float, nullable=True)
    max_adverse_excursion = Column(Float, nullable=True)
    return_pct = Column(Float, nullable=True)

    entry_reached = Column(Integer, default=0)
    target_reached = Column(Integer, default=0)
    stop_loss_reached = Column(Integer, default=0)
    direction_correct = Column(Integer, default=0)
    success = Column(Integer, default=0)

    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TrainingRun(Base):
    """Tracks each training pipeline run with configuration and performance."""
    __tablename__ = "training_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_timestamp = Column(DateTime, nullable=False, index=True)
    config = Column(JSON, nullable=False)
    dataset_size = Column(Integer)
    num_symbols = Column(Integer)
    model_version = Column(String(50), nullable=True)
    walk_forward_metrics = Column(JSON, nullable=True)
    fold_details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ---------- Migration helper ----------
def ensure_schema(engine):
    """Add missing columns to existing tables if needed."""
    inspector = inspect(engine)
    table_name = "prediction_snapshots"
    if not inspector.has_table(table_name):
        return

    existing_columns = [col['name'] for col in inspector.get_columns(table_name)]
    required_columns = {
        'combined_score': 'FLOAT',
        'technical_score': 'FLOAT',
        'fundamental_score': 'FLOAT',
        'news_score': 'FLOAT',
        'prediction_score': 'FLOAT',
        'market_score': 'FLOAT',
        'market_sentiment_adjustment': 'FLOAT',
        'training_score': 'FLOAT',
        'entry_range_low': 'FLOAT',
        'entry_range_high': 'FLOAT',
        'support': 'FLOAT',
        'resistance': 'FLOAT',
        'market_mood': 'VARCHAR(20)',
        'nifty_change_pct': 'FLOAT',
        'sensex_change_pct': 'FLOAT',
        'rsi': 'FLOAT',
        'macd': 'VARCHAR(20)',
        'ema': 'VARCHAR(20)',
        'volume_ratio': 'FLOAT',
        'debt_to_equity': 'FLOAT',
        'roe': 'FLOAT',
        'roce': 'FLOAT',
        'feature_snapshot': 'JSON',
        'model_version': 'VARCHAR(50)',
        't1_success': 'INTEGER',
        't5_success': 'INTEGER',
        'overall_success': 'INTEGER',
    }
    with engine.connect() as conn:
        for col_name, col_type in required_columns.items():
            if col_name not in existing_columns:
                alter_sql = f'ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}'
                conn.execute(text(alter_sql))
                conn.commit()
                print(f"Added column {col_name} to {table_name}")


# ---------- Database setup helpers ----------
def get_engine(database_url="sqlite:///./training.db"):
    return create_engine(database_url, echo=False)

def create_tables(engine):
    Base.metadata.create_all(engine)

def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()