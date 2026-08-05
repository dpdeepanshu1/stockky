"""
Event Tracker Service
-----------------------
Single responsibility: watch for material corporate events (quarterly
results dates, dividends, splits) on subscribed symbols and flag when
something changed since the last check. The Decision Engine and
Notification Service both consume this.

Data source: Yahoo Finance via yfinance (free, keyless) exposes upcoming
earnings dates, dividend history, and stock-split history. This does not
cover every event in the original wishlist (bulk deals, board meetings,
insider trading, exchange filings live on NSE/BSE's own announcement
pages) — those need a scraper against NSE's public corporate-announcements
endpoint, which changes format often enough that it deserves its own
focused build-out. The interface below (`/events/{symbol}`, `/subscribe`,
`/check`) is written so that swapping or adding a data source later is a
one-function change, not a rewrite.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import List

import yfinance as yf
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("event-tracker-service")

app = FastAPI(title="Stockky Event Tracker Service", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATE_PATH = os.getenv("EVENT_STATE_PATH", "/app/state/event_state.json")
os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)


def _load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"subscriptions": [], "last_known": {}}


def _save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


class SubscribeRequest(BaseModel):
    symbols: List[str]


def _normalize(symbol: str) -> str:
    symbol = symbol.strip().upper()
    return symbol if symbol.endswith((".NS", ".BO")) else f"{symbol}.NS"


def _fetch_events(symbol: str) -> dict:
    sym = _normalize(symbol)
    ticker = yf.Ticker(sym)

    next_earnings = None
    try:
        cal = ticker.calendar
        if isinstance(cal, dict) and cal.get("Earnings Date"):
            dates = cal["Earnings Date"]
            next_earnings = str(dates[0]) if isinstance(dates, list) else str(dates)
        elif hasattr(cal, "empty") and not cal.empty and "Earnings Date" in cal.index:
            next_earnings = str(cal.loc["Earnings Date"].iloc[0])
    except Exception as e:
        logger.warning("Could not fetch earnings calendar for %s: %s", sym, e)

    last_dividend = None
    try:
        divs = ticker.dividends
        if not divs.empty:
            last_dividend = {
                "date": str(divs.index[-1].date()),
                "amount": float(divs.iloc[-1]),
            }
    except Exception as e:
        logger.warning("Could not fetch dividends for %s: %s", sym, e)

    last_split = None
    try:
        splits = ticker.splits
        if not splits.empty:
            last_split = {
                "date": str(splits.index[-1].date()),
                "ratio": float(splits.iloc[-1]),
            }
    except Exception as e:
        logger.warning("Could not fetch splits for %s: %s", sym, e)

    return {
        "symbol": sym,
        "next_earnings_date": next_earnings,
        "last_dividend": last_dividend,
        "last_split": last_split,
        "checked_at": datetime.utcnow().isoformat(),
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "event-tracker-service"}


@app.get("/events/{symbol}")
def get_events(symbol: str):
    """Point-in-time event snapshot for one symbol — used by the Decision
    Engine to check 'is there an upcoming earnings date soon' etc."""
    return _fetch_events(symbol)


@app.post("/subscribe")
def subscribe(req: SubscribeRequest):
    state = _load_state()
    existing = set(state["subscriptions"])
    for s in req.symbols:
        existing.add(_normalize(s))
    state["subscriptions"] = sorted(existing)
    _save_state(state)
    return {"subscriptions": state["subscriptions"]}


@app.get("/subscriptions")
def list_subscriptions():
    return {"subscriptions": _load_state()["subscriptions"]}


@app.get("/check")
def check_for_changes():
    """Compare each subscribed symbol's current event snapshot against the
    last known one. Returns only symbols where something material changed —
    this is what the Scheduler/Notification services should poll."""
    state = _load_state()
    changes = []

    for symbol in state["subscriptions"]:
        current = _fetch_events(symbol)
        previous = state["last_known"].get(symbol)

        if previous:
            diff_reasons = []
            if previous.get("next_earnings_date") != current.get("next_earnings_date"):
                diff_reasons.append(
                    f"Earnings date updated: {previous.get('next_earnings_date')} → {current.get('next_earnings_date')}"
                )
            prev_div = previous.get("last_dividend") or {}
            cur_div = current.get("last_dividend") or {}
            if prev_div.get("date") != cur_div.get("date"):
                diff_reasons.append(f"New dividend announced: {cur_div}")
            prev_split = previous.get("last_split") or {}
            cur_split = current.get("last_split") or {}
            if prev_split.get("date") != cur_split.get("date"):
                diff_reasons.append(f"New stock split announced: {cur_split}")

            if diff_reasons:
                changes.append({"symbol": symbol, "changes": diff_reasons})

        state["last_known"][symbol] = current

    _save_state(state)
    return {"checked": len(state["subscriptions"]), "changes": changes}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8006, reload=True)
