"""
Event Tracker Service
-----------------------
Tracks material corporate events for subscribed NSE symbols and flags changes.
State is persisted in Upstash Redis so restarts don't lose subscriptions.

Data sources (all via yfinance — free, no API key):
  - Earnings calendar, dividends, splits            (was already here)
  - Insider transactions & purchases                (new)
  - Analyst upgrades/downgrades                     (new)
  - Institutional holders change                    (new)
  - Latest news headlines                           (new)

NSE/BSE's own announcement endpoints (bulk deals, board meetings, exchange
filings) return 403 from all cloud environments — they require a real browser
session. This service covers the yfinance-accessible subset which is solid
for earnings, dividends, splits, and analyst activity.
"""
import os
import json
import math
import logging
from upstash_redis import Redis
from datetime import datetime
from typing import List

import yfinance as yf
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from upstash_redis import Redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("event-tracker-service")

app = FastAPI(title="Stockky Event Tracker Service", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Redis ──────────────────────────────────────────────────────────────────────
_redis = None
try:
    _redis = Redis(
        url=os.getenv("UPSTASH_REDIS_REST_URL"),
        token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
    )
    _redis.ping()
except Exception as e:
    logger.warning("Redis unavailable, state will not persist: %s", e)

STATE_KEY = "stockky:event_state"

def _load_state() -> dict:
    if _redis:
        try:
            val = _redis.get(STATE_KEY)
            if val:
                return json.loads(val)
        except Exception:
            pass
    return {"subscriptions": [], "last_known": {}}

def _save_state(state: dict):
    if _redis:
        try:
            _redis.set(STATE_KEY, json.dumps(state, default=str))
        except Exception as e:
            logger.warning("Failed to persist state: %s", e)
            
# ── Helpers ────────────────────────────────────────────────────────────────────
class SubscribeRequest(BaseModel):
    symbols: List[str]


def _normalize(symbol: str) -> str:
    symbol = symbol.strip().upper()
    return symbol if symbol.endswith((".NS", ".BO")) else f"{symbol}.NS"


def _safe_float(val) -> float | None:
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# ── Core event fetch ───────────────────────────────────────────────────────────
def _fetch_events(symbol: str) -> dict:
    sym = _normalize(symbol)
    ticker = yf.Ticker(sym)
    ticker._tz = "Asia/Kolkata"

    # 1. Earnings calendar
    next_earnings = None
    try:
        cal = ticker.calendar
        if isinstance(cal, dict) and cal.get("Earnings Date"):
            dates = cal["Earnings Date"]
            next_earnings = str(dates[0]) if isinstance(dates, list) else str(dates)
        elif hasattr(cal, "empty") and not cal.empty and "Earnings Date" in cal.index:
            next_earnings = str(cal.loc["Earnings Date"].iloc[0])
    except Exception as e:
        logger.warning("Earnings calendar unavailable for %s: %s", sym, e)

    # 2. Dividends
    last_dividend = None
    try:
        divs = ticker.dividends
        if divs is not None and not divs.empty:
            last_dividend = {
                "date": str(divs.index[-1].date()),
                "amount": _safe_float(divs.iloc[-1]),
            }
    except Exception as e:
        logger.warning("Dividends unavailable for %s: %s", sym, e)

    # 3. Stock splits
    last_split = None
    try:
        splits = ticker.splits
        if splits is not None and not splits.empty:
            last_split = {
                "date": str(splits.index[-1].date()),
                "ratio": _safe_float(splits.iloc[-1]),
            }
    except Exception as e:
        logger.warning("Splits unavailable for %s: %s", sym, e)

    # 4. Insider transactions (last 3)
    recent_insider = []
    try:
        ins = ticker.insider_transactions
        if ins is not None and not ins.empty:
            for _, row in ins.head(3).iterrows():
                recent_insider.append({
                    "date": str(row.get("Start Date", "")) or str(row.name),
                    "insider": str(row.get("Insider", "")),
                    "transaction": str(row.get("Transaction", "")),
                    "shares": int(row["Shares"]) if "Shares" in row and _safe_float(row["Shares"]) else None,
                    "value": _safe_float(row.get("Value", None)),
                })
    except Exception as e:
        logger.warning("Insider transactions unavailable for %s: %s", sym, e)

    # 5. Analyst upgrades/downgrades (last 3)
    recent_analyst = []
    try:
        ud = ticker.upgrades_downgrades
        if ud is not None and not ud.empty:
            ud_sorted = ud.sort_index(ascending=False)
            for _, row in ud_sorted.head(3).iterrows():
                recent_analyst.append({
                    "date": str(row.name.date()) if hasattr(row.name, "date") else str(row.name),
                    "firm": str(row.get("Firm", "")),
                    "to_grade": str(row.get("ToGrade", "")),
                    "from_grade": str(row.get("FromGrade", "")),
                    "action": str(row.get("Action", "")),
                })
    except Exception as e:
        logger.warning("Upgrades/downgrades unavailable for %s: %s", sym, e)

    # 6. Institutional holders snapshot (top 5)
    institutional_holders = []
    try:
        ih = ticker.institutional_holders
        if ih is not None and not ih.empty:
            for _, row in ih.head(5).iterrows():
                institutional_holders.append({
                    "holder": str(row.get("Holder", "")),
                    "shares": int(row["Shares"]) if "Shares" in row and _safe_float(row.get("Shares")) else None,
                    "pct_held": _safe_float(row.get("% Out", None)),
                })
    except Exception as e:
        logger.warning("Institutional holders unavailable for %s: %s", sym, e)

    # 7. Latest news headlines (last 5)
    recent_news = []
    try:
        news = ticker.news
        if news:
            for item in news[:5]:
                recent_news.append({
                    "title": item.get("content", {}).get("title") or item.get("title", ""),
                    "publisher": item.get("content", {}).get("provider", {}).get("displayName") or item.get("publisher", ""),
                    "published": item.get("content", {}).get("pubDate") or str(item.get("providerPublishTime", "")),
                    "url": item.get("content", {}).get("canonicalUrl", {}).get("url") or item.get("link", ""),
                })
    except Exception as e:
        logger.warning("News unavailable for %s: %s", sym, e)

    return {
        "symbol": sym,
        "next_earnings_date": next_earnings,
        "last_dividend": last_dividend,
        "last_split": last_split,
        "recent_insider_transactions": recent_insider,
        "recent_analyst_actions": recent_analyst,
        "institutional_holders": institutional_holders,
        "recent_news": recent_news,
        "checked_at": datetime.utcnow().isoformat(),
    }


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "event-tracker-service", "redis": bool(_redis)}


@app.get("/events/{symbol}")
def get_events(symbol: str):
    """Full event snapshot for one symbol — consumed by Decision Engine."""
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
    """Diff each subscribed symbol against last known snapshot.
    Returns only symbols where something material changed — used by Scheduler."""
    state = _load_state()
    changes = []

    for symbol in state["subscriptions"]:
        current = _fetch_events(symbol)
        previous = state["last_known"].get(symbol, {})

        diff_reasons = []

        if previous.get("next_earnings_date") != current.get("next_earnings_date"):
            diff_reasons.append(
                f"Earnings date updated: {previous.get('next_earnings_date')} → {current.get('next_earnings_date')}"
            )

        prev_div = (previous.get("last_dividend") or {})
        cur_div  = (current.get("last_dividend") or {})
        if prev_div.get("date") != cur_div.get("date"):
            diff_reasons.append(f"New dividend: {cur_div}")

        prev_split = (previous.get("last_split") or {})
        cur_split  = (current.get("last_split") or {})
        if prev_split.get("date") != cur_split.get("date"):
            diff_reasons.append(f"New split: {cur_split}")

        prev_analyst = [a.get("date") + a.get("firm", "") for a in (previous.get("recent_analyst_actions") or [])]
        cur_analyst  = [a.get("date") + a.get("firm", "") for a in (current.get("recent_analyst_actions") or [])]
        new_analyst  = [a for a in (current.get("recent_analyst_actions") or []) if a.get("date") + a.get("firm", "") not in prev_analyst]
        for action in new_analyst:
            diff_reasons.append(
                f"Analyst action: {action.get('firm')} {action.get('action')} → {action.get('to_grade')}"
            )

        if diff_reasons:
            changes.append({"symbol": symbol, "changes": diff_reasons, "current": current})

        state["last_known"][symbol] = current

    _save_state(state)
    return {"checked": len(state["subscriptions"]), "changes": changes}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8006, reload=True)