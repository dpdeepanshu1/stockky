"""
Event Tracker Service v0.3.2
-----------------------------
Tracks material corporate events for subscribed NSE symbols.
State is persisted in Upstash Redis so restarts don't lose subscriptions.

v0.3.2 changes:
  - Enhanced news fetching: fallback to Google News RSS if yfinance news empty
  - Merges news from both sources (deduplicated by title)
  - Increased max news items to 10
"""
import os
import json
import math
import time
import random
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
from urllib.parse import quote

import yfinance as yf
import feedparser
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from upstash_redis import Redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("event-tracker-service")

app = FastAPI(title="Stockky Event Tracker Service", version="0.3.2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

EVENT_CACHE_TTL = 4 * 3600          # 4 hours — events don't change minute-to-minute
EVENT_FALLBACK_TTL = 30 * 24 * 3600 # 30 days for last‑known‑good fallback
STATE_KEY = "stockky:event_state"
EVENT_CACHE_PREFIX = "stockky:event:"
EVENT_FALLBACK_PREFIX = "stockky:event:fallback:"
EVENTS_LIST_CACHE_KEY = "stockky:events_list"
EVENTS_LIST_CACHE_TTL = 3600        # 1 hour

# ── Redis ──────────────────────────────────────────────────────────────────────
_redis = None
try:
    _redis = Redis(
        url=os.getenv("UPSTASH_REDIS_REST_URL"),
        token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
    )
    _redis.ping()
    logger.info("Connected to Upstash Redis")
except Exception as e:
    logger.warning("Redis unavailable, caching and persistence disabled: %s", e)


def _redis_get(key: str):
    if not _redis:
        return None
    try:
        val = _redis.get(key)
        return json.loads(val) if val else None
    except Exception:
        return None


def _redis_set(key: str, value, ttl: int = None):
    if not _redis:
        return
    try:
        data = json.dumps(value, default=str)
        if ttl:
            _redis.setex(key, ttl, data)
        else:
            _redis.set(key, data)
    except Exception as e:
        logger.warning("Redis set failed for %s: %s", key, e)


def _load_state() -> dict:
    return _redis_get(STATE_KEY) or {"subscriptions": [], "last_known": {}}


def _save_state(state: dict):
    _redis_set(STATE_KEY, state)


# ── Helpers ────────────────────────────────────────────────────────────────────
class SubscribeRequest(BaseModel):
    symbols: List[str]


def _normalize(symbol: str) -> str:
    symbol = symbol.strip().upper()
    return symbol if symbol.endswith((".NS", ".BO")) else f"{symbol}.NS"


def _safe_float(val):
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _yf_call(fn, label: str, sym: str, max_retries: int = 3, base_delay: float = 2):
    """Safely call a yfinance property, retrying transient failures with
    jittered backoff, returning None only if every attempt fails."""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            if attempt == max_retries - 1:
                logger.warning("%s unavailable for %s after %d attempts: %s", label, sym, max_retries, e)
                return None
            wait = random.uniform(0, base_delay * (2 ** attempt))
            logger.info("%s retry %d/%d for %s after %.1fs: %s", label, attempt + 1, max_retries, sym, wait, e)
            time.sleep(wait)


# ── NEW: Fetch news from Google News RSS ──
def _fetch_google_news(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
    """Fetch news headlines from Google News RSS for the given symbol."""
    # Map symbol to a readable company name (use common name for better search)
    name_hints = {
        "TCS": "Tata Consultancy Services",
        "INFY": "Infosys",
        "HDFCBANK": "HDFC Bank",
        "ICICIBANK": "ICICI Bank",
        "RELIANCE": "Reliance Industries",
        "HCLTECH": "HCL Technologies",
        "COFORGE": "Coforge",
        "ANGELONE": "Angel One",
        "ADANIPOWER": "Adani Power",
        "BEL": "Bharat Electronics",
        "HAL": "Hindustan Aeronautics",
        "TATAMOTORS": "Tata Motors",
        "SBIN": "State Bank of India",
        "PWL": "PhysicsWallah",
    }
    base = symbol.replace(".NS", "").replace(".BO", "").upper()
    query = name_hints.get(base, base) + " NSE stock"
    encoded_query = quote(query)
    feed_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-IN&gl=IN&ceid=IN:en"

    try:
        parsed = feedparser.parse(feed_url)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            logger.warning("Google News RSS feed returned empty for %s", symbol)
            return []

        items = []
        cutoff = datetime.utcnow() - timedelta(days=10)  # only last 10 days
        for entry in parsed.entries[:max_items]:
            published = None
            if getattr(entry, "published_parsed", None):
                published = datetime(*entry.published_parsed[:6])
            if published and published < cutoff:
                continue
            items.append({
                "title": entry.title,
                "publisher": getattr(entry.source, "title", None) if hasattr(entry, "source") else None,
                "published": published.isoformat() if published else None,
                "url": entry.link,
            })
        return items
    except Exception as e:
        logger.warning("Failed to fetch Google News for %s: %s", symbol, e)
        return []


# ── Core event fetch (with Redis cache and fallback) ─────────────────────────
def _fetch_events(symbol: str, force: bool = False) -> dict:
    sym = _normalize(symbol)
    cache_key = f"{EVENT_CACHE_PREFIX}{sym}"

    # Return cached data if available and not forced
    if not force:
        cached = _redis_get(cache_key)
        if cached:
            logger.info("Event cache hit for %s", sym)
            return cached

    logger.info("Fetching fresh events for %s from yfinance", sym)
    ticker = yf.Ticker(sym)
    ticker._tz = "Asia/Kolkata"

    # 1. Earnings calendar (using get_earnings_dates for reliability)
    next_earnings = None
    earnings_dates = _yf_call(lambda: ticker.get_earnings_dates(limit=1), "Earnings dates", sym)
    if earnings_dates is not None and not earnings_dates.empty:
        try:
            next_earnings = str(earnings_dates.index[0].date())
        except Exception:
            pass

    # 2. Dividends
    last_dividend = None
    divs = _yf_call(lambda: ticker.dividends, "Dividends", sym)
    if divs is not None and not divs.empty:
        try:
            last_dividend = {
                "date": str(divs.index[-1].date()),
                "amount": _safe_float(divs.iloc[-1]),
            }
        except Exception:
            pass

    # 3. Splits
    last_split = None
    splits = _yf_call(lambda: ticker.splits, "Splits", sym)
    if splits is not None and not splits.empty:
        try:
            last_split = {
                "date": str(splits.index[-1].date()),
                "ratio": _safe_float(splits.iloc[-1]),
            }
        except Exception:
            pass

    # 4. Insider transactions (last 3)
    recent_insider = []
    ins = _yf_call(lambda: ticker.insider_transactions, "Insider transactions", sym)
    if ins is not None and not ins.empty:
        try:
            for _, row in ins.head(3).iterrows():
                recent_insider.append({
                    "date": str(row.get("Start Date", "")) or str(row.name),
                    "insider": str(row.get("Insider", "")),
                    "transaction": str(row.get("Transaction", "")),
                    "shares": int(row["Shares"]) if "Shares" in row and _safe_float(row.get("Shares")) else None,
                    "value": _safe_float(row.get("Value")),
                })
        except Exception:
            pass

    # 5. Analyst upgrades/downgrades (last 3)
    recent_analyst = []
    ud = _yf_call(lambda: ticker.upgrades_downgrades, "Upgrades/downgrades", sym)
    if ud is not None and not ud.empty:
        try:
            ud_sorted = ud.sort_index(ascending=False)
            for _, row in ud_sorted.head(3).iterrows():
                recent_analyst.append({
                    "date": str(row.name.date()) if hasattr(row.name, "date") else str(row.name),
                    "firm": str(row.get("Firm", "")),
                    "to_grade": str(row.get("ToGrade", "")),
                    "from_grade": str(row.get("FromGrade", "")),
                    "action": str(row.get("Action", "")),
                })
        except Exception:
            pass

    # 6. Institutional holders (top 5)
    institutional_holders = []
    ih = _yf_call(lambda: ticker.institutional_holders, "Institutional holders", sym)
    if ih is not None and not ih.empty:
        try:
            for _, row in ih.head(5).iterrows():
                institutional_holders.append({
                    "holder": str(row.get("Holder", "")),
                    "shares": int(row["Shares"]) if "Shares" in row and _safe_float(row.get("Shares")) else None,
                    "pct_held": _safe_float(row.get("% Out")),
                })
        except Exception:
            pass

    # ── 7. News headlines: first try yfinance, then fallback to Google News ──
    recent_news = []
    yf_news = _yf_call(lambda: ticker.news, "News", sym)
    if yf_news:
        try:
            for item in yf_news[:5]:
                recent_news.append({
                    "title": item.get("content", {}).get("title") or item.get("title", ""),
                    "publisher": (item.get("content", {}).get("provider", {}) or {}).get("displayName") or item.get("publisher", ""),
                    "published": item.get("content", {}).get("pubDate") or str(item.get("providerPublishTime", "")),
                    "url": (item.get("content", {}).get("canonicalUrl", {}) or {}).get("url") or item.get("link", ""),
                })
        except Exception:
            pass

    # If yfinance returned fewer than 3 news, supplement with Google News
    if len(recent_news) < 3:
        logger.info("yfinance news had only %d items, fetching from Google News RSS", len(recent_news))
        google_news = _fetch_google_news(sym, max_items=10)
        # Deduplicate by title (case-insensitive)
        existing_titles = {n["title"].lower() for n in recent_news if n["title"]}
        for item in google_news:
            if item["title"].lower() not in existing_titles:
                recent_news.append(item)
                existing_titles.add(item["title"].lower())
                if len(recent_news) >= 10:
                    break

    # 8. Earnings surprise (new)
    earnings_surprise = None
    earnings_history = _yf_call(lambda: ticker.earnings_history, "Earnings history", sym)
    if earnings_history is not None and not earnings_history.empty:
        try:
            latest = earnings_history.iloc[0]
            actual = latest.get("actual")
            estimate = latest.get("estimate")
            if actual is not None and estimate is not None and estimate != 0:
                surprise_pct = ((actual - estimate) / estimate) * 100
                earnings_surprise = {
                    "date": str(latest.name),
                    "actual": _safe_float(actual),
                    "estimate": _safe_float(estimate),
                    "surprise_pct": round(surprise_pct, 2)
                }
        except Exception:
            pass

    # 9. Bulk/Block deals (placeholder – can be extended later)
    bulk_deals = []

    # 10. FII/DII Net Flow (placeholder)
    fii_dii_net_flow = None

    result = {
        "symbol": sym,
        "next_earnings_date": next_earnings,
        "last_dividend": last_dividend,
        "last_split": last_split,
        "recent_insider_transactions": recent_insider,
        "recent_analyst_actions": recent_analyst,
        "institutional_holders": institutional_holders,
        "recent_news": recent_news,
        "earnings_surprise": earnings_surprise,
        "bulk_deals": bulk_deals,
        "fii_dii_net_flow": fii_dii_net_flow,
        "checked_at": datetime.utcnow().isoformat(),
        "cached": False,
    }

    fallback_key = f"{EVENT_FALLBACK_PREFIX}{sym}"
    has_real_data = any([
        next_earnings, last_dividend, last_split,
        recent_insider, recent_analyst, institutional_holders, recent_news,
        earnings_surprise, bulk_deals, fii_dii_net_flow,
    ])

    if has_real_data:
        # Cache good data with normal TTL and also store as fallback
        _redis_set(cache_key, {**result, "cached": True}, ttl=EVENT_CACHE_TTL)
        _redis_set(fallback_key, result, ttl=EVENT_FALLBACK_TTL)
        return result

    # Live fetch returned empty – try to serve last‑known‑good fallback
    stale = _redis_get(fallback_key)
    if stale:
        logger.info("Live fetch for %s came back empty; serving last-known-good fallback", sym)
        stale = {**stale, "cached": True, "stale": True}
        # Short TTL on the poisoned cache so we keep retrying the live path soon
        _redis_set(cache_key, stale, ttl=900)
        return stale

    # Genuinely nothing available – return empty result
    return result


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "service": "Stockky Event Tracker Service",
        "version": "0.3.2",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/events/{symbol}": "GET – full event snapshot",
            "/events/{symbol}?force=true": "GET – bypass cache",
            "/subscribe": "POST – subscribe symbols",
            "/subscriptions": "GET – list subscriptions",
            "/check": "GET – check for changes",
            "/symbols_with_events": "GET – list symbols with upcoming events",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "event-tracker-service", "redis": bool(_redis)}


@app.get("/events/{symbol}")
def get_events(symbol: str, force: bool = False):
    """Full event snapshot for one symbol. Cached in Redis for 4 hours."""
    return _fetch_events(symbol, force=force)


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
    Staggered with 1s delay between symbols to avoid Yahoo rate limits.
    Uses 4hr cache — only fetches fresh data if cache is expired."""
    state = _load_state()
    changes = []

    for i, symbol in enumerate(state["subscriptions"]):
        # Stagger requests: 1 second apart to avoid rate limits
        if i > 0:
            time.sleep(1)

        current = _fetch_events(symbol)
        previous = state["last_known"].get(symbol, {})

        diff_reasons = []

        # Earnings date changed
        if previous.get("next_earnings_date") != current.get("next_earnings_date"):
            diff_reasons.append(
                f"Earnings date: {previous.get('next_earnings_date')} → {current.get('next_earnings_date')}"
            )

        # New dividend
        prev_div = previous.get("last_dividend") or {}
        cur_div  = current.get("last_dividend") or {}
        if prev_div.get("date") != cur_div.get("date") and cur_div.get("date"):
            diff_reasons.append(f"New dividend declared: ₹{cur_div.get('amount')} on {cur_div.get('date')}")

        # New split
        prev_split = previous.get("last_split") or {}
        cur_split  = current.get("last_split") or {}
        if prev_split.get("date") != cur_split.get("date") and cur_split.get("date"):
            diff_reasons.append(f"Stock split: {cur_split.get('ratio')}:1 on {cur_split.get('date')}")

        # New analyst actions
        prev_keys = {
            (a.get("date", "") + a.get("firm", ""))
            for a in (previous.get("recent_analyst_actions") or [])
        }
        for action in (current.get("recent_analyst_actions") or []):
            key = action.get("date", "") + action.get("firm", "")
            if key not in prev_keys:
                diff_reasons.append(
                    f"Analyst: {action.get('firm')} {action.get('action')} → {action.get('to_grade')}"
                )

        # New insider transactions
        prev_insider_keys = {
            (a.get("date", "") + a.get("insider", ""))
            for a in (previous.get("recent_insider_transactions") or [])
        }
        for txn in (current.get("recent_insider_transactions") or []):
            key = txn.get("date", "") + txn.get("insider", "")
            if key not in prev_insider_keys:
                diff_reasons.append(
                    f"Insider {txn.get('transaction')}: {txn.get('insider')} — {txn.get('shares')} shares"
                )

        # Earnings surprise changed
        prev_surprise = previous.get("earnings_surprise") or {}
        cur_surprise = current.get("earnings_surprise") or {}
        if prev_surprise.get("surprise_pct") != cur_surprise.get("surprise_pct"):
            diff_reasons.append(
                f"Earnings surprise: {cur_surprise.get('surprise_pct')}%"
            )

        # Bulk/Block deals
        prev_bulk = previous.get("bulk_deals") or []
        cur_bulk = current.get("bulk_deals") or []
        if len(cur_bulk) != len(prev_bulk):
            diff_reasons.append(f"Bulk/Block deal detected")

        if diff_reasons:
            changes.append({"symbol": symbol, "changes": diff_reasons, "current": current})

        state["last_known"][symbol] = current

    _save_state(state)
    return {
        "checked": len(state["subscriptions"]),
        "changes": changes,
        "checked_at": datetime.utcnow().isoformat(),
    }


# ── symbols_with_events endpoint ──────────────────────────────────────────
@app.get("/symbols_with_events")
def symbols_with_events(days_ahead: int = 7):
    """Return a list of subscribed symbols that have an upcoming event
    (earnings, dividend, split) within the next `days_ahead` days.
    The list is cached in Redis for 1 hour."""
    # Try to serve from cache
    cached = _redis_get(EVENTS_LIST_CACHE_KEY)
    if cached and isinstance(cached, list):
        logger.info("Serving cached symbols_with_events list")
        return {"symbols": cached}

    state = _load_state()
    subscriptions = state.get("subscriptions", [])
    if not subscriptions:
        logger.info("No subscriptions, returning empty list")
        _redis_set(EVENTS_LIST_CACHE_KEY, [], ttl=EVENTS_LIST_CACHE_TTL)
        return {"symbols": []}

    now = datetime.utcnow()
    cutoff = now + timedelta(days=days_ahead)
    result_symbols = []

    for symbol in subscriptions:
        # Fetch cached event data (do not force fresh fetch)
        cache_key = f"{EVENT_CACHE_PREFIX}{symbol}"
        cached_events = _redis_get(cache_key)
        if not cached_events:
            # If not cached, skip – we don't want to trigger expensive fetches
            continue

        next_earnings = cached_events.get("next_earnings_date")
        if next_earnings:
            try:
                dt = datetime.fromisoformat(next_earnings)
                if now <= dt <= cutoff:
                    result_symbols.append(symbol)
                    continue
            except (ValueError, TypeError):
                pass

        # Check dividends
        last_div = cached_events.get("last_dividend")
        if last_div and last_div.get("date"):
            try:
                dt = datetime.fromisoformat(last_div["date"])
                if now <= dt <= cutoff:
                    result_symbols.append(symbol)
                    continue
            except (ValueError, TypeError):
                pass

        # Check splits
        last_split = cached_events.get("last_split")
        if last_split and last_split.get("date"):
            try:
                dt = datetime.fromisoformat(last_split["date"])
                if now <= dt <= cutoff:
                    result_symbols.append(symbol)
                    continue
            except (ValueError, TypeError):
                pass

    # Deduplicate and sort
    result_symbols = sorted(set(result_symbols))
    # Cache for 1 hour
    _redis_set(EVENTS_LIST_CACHE_KEY, result_symbols, ttl=EVENTS_LIST_CACHE_TTL)
    logger.info(f"Returning {len(result_symbols)} symbols with upcoming events")
    return {"symbols": result_symbols}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8006))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)