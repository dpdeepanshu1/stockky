# services/training-service/models.py
"""
SQLAlchemy models for the training service.

Tables:
- PredictionSnapshot: stores the feature snapshot at prediction time (immutable).
- PredictionOutcome: stores T+1 and T+5 evaluation results.
- TrainingRun: tracks each training pipeline run (for auditing and metrics).
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime, JSON, Text, LargeBinary,
    create_engine, inspect, text, desc
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import numpy as np

# ---------- IST timezone helper ----------
IST = ZoneInfo("Asia/Kolkata")

def ist_now() -> datetime:
    """Return current time as a naive datetime in IST (UTC+5:30)."""
    return datetime.now(IST).replace(tzinfo=None)

# ---------- Base ----------
Base = declarative_base()


# ---------- Numpy conversion helper ----------
def convert_numpy(obj):
    """Recursively convert numpy types to Python native types."""
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: convert_numpy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_numpy(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    return obj


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
    created_at = Column(DateTime, default=ist_now)

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
    created_at = Column(DateTime, default=ist_now)


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
    created_at = Column(DateTime, default=ist_now)


class ModelArtifact(Base):
    """
    The actual trained model, stored IN the database rather than on local
    disk. This is the piece that was missing entirely before: training-
    service and prediction-service are separate Render containers with no
    shared filesystem, so a model saved to local disk was never reachable
    by anything else — and on free tier, not even reachable by
    training-service's own NEXT restart. Storing the serialized bytes as a
    row here means any service with this DATABASE_URL (or a REST call to
    training-service) can retrieve the current production model, and nothing
    is lost on restart.

    A version is a simple auto-incrementing string ("v1", "v2", ...).
    Exactly one row can have status="production" at a time — enforced in
    ModelRegistry.save_production_model()/promote_model(), not at the DB
    level, since SQLite (used for local dev without DATABASE_URL set)
    doesn't support partial unique indexes as portably as Postgres does.
    """
    __tablename__ = "model_artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(String(20), unique=True, nullable=False, index=True)
    status = Column(String(20), nullable=False, default="candidate", index=True)  # candidate | production | archived
    model_blob = Column(LargeBinary, nullable=False)
    scaler_blob = Column(LargeBinary, nullable=True)
    feature_columns = Column(JSON, nullable=True)
    config = Column(JSON, nullable=True)
    metrics = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=ist_now, index=True)
    promoted_at = Column(DateTime, nullable=True)


class ModelRegistry:
    """
    Postgres-backed model store. Replaces the local-disk-file design that
    was referenced throughout app.py/train.py (registry.model_dir,
    {version}.pkl, production_pointer.json) but never actually implemented
    — the `from models import ModelRegistry` import always failed, so
    HAS_MODEL_REGISTRY was always False and every training run silently
    fell into a "legacy" path that saved to an unreachable local file.

    Interface kept close to what the calling code already expects, so
    app.py/train.py needed only their file-scanning logic updated to call
    these methods instead of touching the filesystem — not a rewrite of
    the training pipeline itself.
    """

    def __init__(self, session_factory=None):
        """session_factory: a SQLAlchemy sessionmaker. If omitted, builds
        its own from DATABASE_URL — needed because train.py's call site
        does `ModelRegistry()` with no arguments. Passing one in explicitly
        (as app.py does, reusing its existing SessionLocal) avoids opening
        a second, redundant DB connection pool."""
        if session_factory is None:
            import os
            db_url = os.environ.get("DATABASE_URL", "sqlite:///./training.db")
            engine = create_engine(db_url, echo=False)
            Base.metadata.create_all(engine)
            session_factory = sessionmaker(bind=engine)
        self._session_factory = session_factory

    def _next_version(self, session) -> str:
        latest = session.query(ModelArtifact).order_by(desc(ModelArtifact.id)).first()
        n = 1
        if latest and latest.version.startswith("v"):
            try:
                n = int(latest.version[1:]) + 1
            except ValueError:
                n = (latest.id or 0) + 1
        return f"v{n}"

    def save_production_model(self, model, scaler, config: dict, metrics: dict, feature_columns=None) -> str:
        """Serializes and saves a model directly as the new production
        version, archiving whatever was production before. Matches the
        existing call site in train.py, which trains on the full dataset
        and treats the result as immediately deployable (no separate
        candidate/promote step in that pipeline today)."""
        return self._save(model, scaler, config, metrics, feature_columns, status="production")

    def save_candidate_model(self, model, scaler, config: dict, metrics: dict, feature_columns=None) -> str:
        """For a future safer workflow: train, inspect metrics, THEN
        promote explicitly via promote_model() instead of going live
        immediately."""
        return self._save(model, scaler, config, metrics, feature_columns, status="candidate")

    def _save(self, model, scaler, config, metrics, feature_columns, status) -> str:
        import io
        import joblib

        model_buf = io.BytesIO()
        joblib.dump(model, model_buf)
        model_bytes = model_buf.getvalue()

        scaler_bytes = None
        if scaler is not None:
            scaler_buf = io.BytesIO()
            joblib.dump(scaler, scaler_buf)
            scaler_bytes = scaler_buf.getvalue()

        # 🔥 Convert numpy types to Python native for JSON serialization
        config_sanitized = convert_numpy(config)
        metrics_sanitized = convert_numpy(metrics)

        session = self._session_factory()
        try:
            version = self._next_version(session)
            artifact = ModelArtifact(
                version=version,
                status=status,
                model_blob=model_bytes,
                scaler_blob=scaler_bytes,
                feature_columns=feature_columns,
                config=config_sanitized,
                metrics=metrics_sanitized,
                created_at=ist_now(),
                promoted_at=ist_now() if status == "production" else None,
            )
            if status == "production":
                # Exactly one production row at a time — archive the rest.
                session.query(ModelArtifact).filter(
                    ModelArtifact.status == "production"
                ).update({"status": "archived"})
            session.add(artifact)
            session.commit()
            return version
        finally:
            session.close()

    def promote_model(self, version: str) -> bool:
        """Promotes an existing candidate (or archived) version to
        production, archiving whatever was production before. Returns
        False if the version doesn't exist."""
        session = self._session_factory()
        try:
            target = session.query(ModelArtifact).filter(ModelArtifact.version == version).first()
            if not target:
                return False
            session.query(ModelArtifact).filter(
                ModelArtifact.status == "production"
            ).update({"status": "archived"})
            target.status = "production"
            target.promoted_at = ist_now()
            session.commit()
            return True
        finally:
            session.close()

    def get_production_model(self):
        """Returns (model, scaler, metadata_dict) for the current
        production model, or None if none has ever been promoted."""
        import io
        import joblib

        session = self._session_factory()
        try:
            artifact = session.query(ModelArtifact).filter(
                ModelArtifact.status == "production"
            ).order_by(desc(ModelArtifact.promoted_at)).first()
            if not artifact:
                return None
            model = joblib.load(io.BytesIO(artifact.model_blob))
            scaler = joblib.load(io.BytesIO(artifact.scaler_blob)) if artifact.scaler_blob else None
            meta = {
                "version": artifact.version,
                "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
                "promoted_at": artifact.promoted_at.isoformat() if artifact.promoted_at else None,
                "feature_columns": artifact.feature_columns,
                "config": artifact.config,
                "metrics": artifact.metrics,
            }
            return model, scaler, meta
        finally:
            session.close()

    def list_models(self):
        """Metadata only (no blobs) for every version, newest first —
        what /api/models and the Training tab's history view need."""
        session = self._session_factory()
        try:
            rows = session.query(ModelArtifact).order_by(desc(ModelArtifact.created_at)).all()
            return [
                {
                    "version": r.version,
                    "status": r.status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "promoted_at": r.promoted_at.isoformat() if r.promoted_at else None,
                    "metrics": r.metrics,
                    "config": r.config,
                }
                for r in rows
            ]
        finally:
            session.close()


# ---------- Migration helper ----------
def ensure_schema(engine):
    """Add missing columns to existing tables if needed."""
    inspector = inspect(engine)

    # ---- prediction_snapshots ----
    table_name = "prediction_snapshots"
    if inspector.has_table(table_name):
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

    # ---- training_runs ----
    table_name = "training_runs"
    if inspector.has_table(table_name):
        existing_columns = [col['name'] for col in inspector.get_columns(table_name)]
        # All columns defined in TrainingRun model
        required_columns = {
            'run_timestamp': 'TIMESTAMP',
            'config': 'JSON',
            'dataset_size': 'INTEGER',
            'num_symbols': 'INTEGER',
            'model_version': 'VARCHAR(50)',
            'walk_forward_metrics': 'JSON',
            'fold_details': 'JSON',
            'created_at': 'TIMESTAMP',
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