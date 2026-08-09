"""
Outcome evaluation for predictions.
"""
import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
import yfinance as yf

# ✅ Absolute import
import models as db_models

logger = logging.getLogger("training-service.evaluate")

def evaluate_t1(prediction_id: str):
    """Evaluate a prediction on T+1 (next trading day)."""
    db = Session()
    try:
        # Get the prediction snapshot
        pred = db.query(db_models.PredictionSnapshot).filter(
            db_models.PredictionSnapshot.prediction_id == prediction_id
        ).first()
        if not pred:
            logger.warning(f"Prediction {prediction_id} not found")
            return

        # Check if already evaluated
        existing = db.query(db_models.PredictionOutcome).filter(
            db_models.PredictionOutcome.prediction_id == prediction_id,
            db_models.PredictionOutcome.evaluation_period == 'T+1'
        ).first()
        if existing:
            return

        # Fetch next day's data
        symbol = pred.symbol + ".NS"  # Yahoo Finance suffix
        start_date = pred.timestamp.date()
        end_date = start_date + timedelta(days=5)  # Fetch a few days to get next trading day
        
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start_date, end=end_date)
        
        if len(hist) < 2:
            logger.warning(f"Not enough data for {symbol} on T+1")
            return

        # Get the next trading day data (index 1)
        next_day = hist.iloc[1]
        open_price = next_day['Open']
        high = next_day['High']
        low = next_day['Low']
        close = next_day['Close']
        
        # Compute metrics
        entry_price = pred.price
        max_favorable = max(high - entry_price, 0) / entry_price * 100
        max_adverse = max(entry_price - low, 0) / entry_price * 100
        return_pct = (close - entry_price) / entry_price * 100
        
        entry_reached = 1 if (low <= entry_price <= high) else 0
        target_reached = 1 if (pred.target and high >= pred.target) else 0
        stop_loss_reached = 1 if (pred.stop_loss and low <= pred.stop_loss) else 0
        direction_correct = 1 if (return_pct > 0) else 0
        success = 1 if (target_reached or (direction_correct and return_pct > 1.0)) else 0

        # Save outcome
        outcome = db_models.PredictionOutcome(
            prediction_id=prediction_id,
            evaluation_period='T+1',
            evaluation_date=next_day.name.to_pydatetime(),
            open_price=open_price,
            high_price=high,
            low_price=low,
            close_price=close,
            max_favorable_excursion=round(max_favorable, 2),
            max_adverse_excursion=round(max_adverse, 2),
            return_pct=round(return_pct, 2),
            entry_reached=entry_reached,
            target_reached=target_reached,
            stop_loss_reached=stop_loss_reached,
            direction_correct=direction_correct,
            success=success
        )
        db.add(outcome)
        db.commit()
        logger.info(f"T+1 evaluation completed for {prediction_id}")
        
    except Exception as e:
        logger.error(f"Error evaluating T+1 for {prediction_id}: {e}")
        db.rollback()
    finally:
        db.close()

def evaluate_t5(prediction_id: str):
    """Evaluate a prediction on T+5 (approximately one week)."""
    db = Session()
    try:
        # Similar to evaluate_t1 but fetch 10 days of data and find the 5th trading day
        pred = db.query(db_models.PredictionSnapshot).filter(
            db_models.PredictionSnapshot.prediction_id == prediction_id
        ).first()
        if not pred:
            return

        existing = db.query(db_models.PredictionOutcome).filter(
            db_models.PredictionOutcome.prediction_id == prediction_id,
            db_models.PredictionOutcome.evaluation_period == 'T+5'
        ).first()
        if existing:
            return

        symbol = pred.symbol + ".NS"
        start_date = pred.timestamp.date()
        end_date = start_date + timedelta(days=15)
        
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start_date, end=end_date)
        
        if len(hist) < 6:
            logger.warning(f"Not enough data for {symbol} on T+5")
            return

        # Get the 5th trading day (index 5)
        t5_day = hist.iloc[5] if len(hist) > 5 else hist.iloc[-1]
        open_price = t5_day['Open']
        high = t5_day['High']
        low = t5_day['Low']
        close = t5_day['Close']
        
        # Compute metrics over the 5-day period
        entry_price = pred.price
        period_high = hist['High'].iloc[1:6].max() if len(hist) > 5 else hist['High'].max()
        period_low = hist['Low'].iloc[1:6].min() if len(hist) > 5 else hist['Low'].min()
        
        max_favorable = max(period_high - entry_price, 0) / entry_price * 100
        max_adverse = max(entry_price - period_low, 0) / entry_price * 100
        return_pct = (close - entry_price) / entry_price * 100
        
        target_reached = 1 if (pred.target and period_high >= pred.target) else 0
        stop_loss_reached = 1 if (pred.stop_loss and period_low <= pred.stop_loss) else 0
        direction_correct = 1 if (return_pct > 0) else 0
        success = 1 if (target_reached or (direction_correct and return_pct > 2.0)) else 0

        outcome = db_models.PredictionOutcome(
            prediction_id=prediction_id,
            evaluation_period='T+5',
            evaluation_date=t5_day.name.to_pydatetime(),
            open_price=open_price,
            high_price=high,
            low_price=low,
            close_price=close,
            max_favorable_excursion=round(max_favorable, 2),
            max_adverse_excursion=round(max_adverse, 2),
            return_pct=round(return_pct, 2),
            entry_reached=1,  # Assuming entry was possible
            target_reached=target_reached,
            stop_loss_reached=stop_loss_reached,
            direction_correct=direction_correct,
            success=success
        )
        db.add(outcome)
        db.commit()
        logger.info(f"T+5 evaluation completed for {prediction_id}")
        
    except Exception as e:
        logger.error(f"Error evaluating T+5 for {prediction_id}: {e}")
        db.rollback()
    finally:
        db.close()