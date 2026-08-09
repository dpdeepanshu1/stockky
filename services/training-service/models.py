"""
Database models for the Training Service.
"""
from sqlalchemy import Column, String, Float, Integer, DateTime, JSON, Text
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class PredictionSnapshot(Base):
    __tablename__ = "prediction_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    prediction_id = Column(String, unique=True, index=True)
    symbol = Column(String, index=True)
    timestamp = Column(DateTime)
    price = Column(Float)
    decision = Column(String)
    confidence = Column(Float)
    entry_range = Column(String, nullable=True)
    target = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    market_sentiment = Column(JSON)
    features = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)

class PredictionOutcome(Base):
    __tablename__ = "prediction_outcomes"

    id = Column(Integer, primary_key=True, index=True)
    prediction_id = Column(String, index=True)
    evaluation_period = Column(String)  # 'T+1', 'T+5'
    evaluation_date = Column(DateTime)
    open_price = Column(Float, nullable=True)
    high_price = Column(Float, nullable=True)
    low_price = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)
    max_favorable_excursion = Column(Float, nullable=True)
    max_adverse_excursion = Column(Float, nullable=True)
    return_pct = Column(Float, nullable=True)
    entry_reached = Column(Integer, default=0)  # boolean
    target_reached = Column(Integer, default=0)  # boolean
    stop_loss_reached = Column(Integer, default=0)  # boolean
    direction_correct = Column(Integer, default=0)  # boolean
    success = Column(Integer, default=0)  # boolean

class TrainingRun(Base):
    __tablename__ = "training_runs"

    id = Column(Integer, primary_key=True, index=True)
    run_date = Column(DateTime, default=datetime.now)
    model_version = Column(String)
    dataset_start = Column(DateTime)
    dataset_end = Column(DateTime)
    features_used = Column(JSON)
    parameters = Column(JSON)
    metrics = Column(JSON)
    status = Column(String)  # 'running', 'completed', 'failed'

class ModelVersion(Base):
    __tablename__ = "model_versions"

    id = Column(Integer, primary_key=True, index=True)
    version = Column(String, unique=True)
    training_run_id = Column(Integer)
    status = Column(String)  # 'candidate', 'production', 'archived'
    metrics = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)
    promoted_at = Column(DateTime, nullable=True)