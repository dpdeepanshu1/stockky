"""
Event Tracker Service v0.4.1
-----------------------------
Added detailed logging for multi‑source news fetching.
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
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from upstash_redis import Redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("event-tracker-service")

app = FastAPI(title="Stockky Event Tracker Service", version="0.4.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

EVENT_CACHE_TTL = 4 * 3600
EMPTY_NEWS_CACHE_TTL = 3600
EVENT_FALLBACK_TTL = 30 * 24 * 3600
STATE_KEY = "stockky:event_state"
EVENT_CACHE_PREFIX = "stockky:event:"
EVENT_FALLBACK_PREFIX = "stockky:event:fallback:"
EVENTS_LIST_CACHE_KEY = "stockky:events_list"
EVENTS_LIST_CACHE_TTL = 3600

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


# ── News sources ──

def _fetch_google_news(symbol: str, max_items: int = 10) -> List[Dict[str, Any]]:
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
        cutoff = datetime.utcnow() - timedelta(days=30)
        for entry in parsed.entries[:max_items]:
            published = None
            if getattr(entry, "published_parsed", None):
                published = datetime(*entry.published_parsed[:6])
            if published and published < cutoff:
                continue
            items.append({
                "title": entry.title,
                "publisher": getattr(entry.source, "title", None) if hasattr(entry, "source") else "Google News",
                "published": published.isoformat() if published else None,
                "url": entry.link,
            })
        return items
    except Exception as e:
        logger.warning("Failed to fetch Google News for %s: %s", symbol, e)
        return []


def _fetch_moneycontrol_news(symbol: str, max_items: int = 5) -> List[Dict[str, Any]]:
    """Fetch from Moneycontrol RSS with httpx fallback."""
    keyword = symbol.replace(".NS", "").replace(".BO", "").upper()
    feed_url = "https://www.moneycontrol.com/rss/latestnews.xml"
    try:
        # Try feedparser first
        parsed = feedparser.parse(feed_url)
        items = []
        cutoff = datetime.utcnow() - timedelta(days=30)
        for entry in parsed.entries[:50]:
            title = entry.title.lower()
            desc = entry.description.lower() if hasattr(entry, "description") else ""
            if keyword.lower() in title or keyword.lower() in desc:
                published = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                if published and published < cutoff:
                    continue
                items.append({
                    "title": entry.title,
                    "publisher": "Moneycontrol",
                    "published": published.isoformat() if published else None,
                    "url": entry.link,
                })
                if len(items) >= max_items:
                    break
        return items
    except Exception as e:
        logger.warning("Moneycontrol feedparser failed: %s, trying httpx", e)
        # Fallback to httpx
        try:
            with httpx.Client(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
                resp = client.get(feed_url)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.text)
                items = []
                cutoff = datetime.utcnow() - timedelta(days=30)
                for entry in parsed.entries[:50]:
                    title = entry.title.lower()
                    desc = entry.description.lower() if hasattr(entry, "description") else ""
                    if keyword.lower() in title or keyword.lower() in desc:
                        published = None
                        if hasattr(entry, 'published_parsed') and entry.published_parsed:
                            published = datetime(*entry.published_parsed[:6])
                        if published and published < cutoff:
                            continue
                        items.append({
                            "title": entry.title,
                            "publisher": "Moneycontrol",
                            "published": published.isoformat() if published else None,
                            "url": entry.link,
                        })
                        if len(items) >= max_items:
                            break
                return items
        except Exception as e2:
            logger.warning("Moneycontrol httpx fallback also failed: %s", e2)
            return []


def _fetch_economic_times(symbol: str, max_items: int = 5) -> List[Dict[str, Any]]:
    keyword = symbol.replace(".NS", "").replace(".BO", "").upper()
    feed_url = "https://economictimes.indiatimes.com/rssfeedstopstories.cms"
    try:
        parsed = feedparser.parse(feed_url)
        items = []
        cutoff = datetime.utcnow() - timedelta(days=30)
        for entry in parsed.entries[:50]:
            title = entry.title.lower()
            desc = entry.description.lower() if hasattr(entry, "description") else ""
            if keyword.lower() in title or keyword.lower() in desc:
                published = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                if published and published < cutoff:
                    continue
                items.append({
                    "title": entry.title,
                    "publisher": "Economic Times",
                    "published": published.isoformat() if published else None,
                    "url": entry.link,
                })
                if len(items) >= max_items:
                    break
        return items
    except Exception as e:
        logger.warning("Economic Times fetch failed: %s", e)
        return []


def _fetch_cnbc_tv18(symbol: str, max_items: int = 5) -> List[Dict[str, Any]]:
    keyword = symbol.replace(".NS", "").replace(".BO", "").upper()
    feed_url = "https://www.cnbctv18.com/feed/"
    try:
        parsed = feedparser.parse(feed_url)
        items = []
        cutoff = datetime.utcnow() - timedelta(days=30)
        for entry in parsed.entries[:50]:
            title = entry.title.lower()
            desc = entry.description.lower() if hasattr(entry, "description") else ""
            if keyword.lower() in title or keyword.lower() in desc:
                published = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                if published and published < cutoff:
                    continue
                items.append({
                    "title": entry.title,
                    "publisher": "CNBC TV18",
                    "published": published.isoformat() if published else None,
                    "url": entry.link,
                })
                if len(items) >= max_items:
                    break
        return items
    except Exception as e:
        logger.warning("CNBC TV18 fetch failed: %s", e)
        return []


def _fetch_yf_news(symbol: str) -> List[Dict[str, Any]]:
    ticker = yf.Ticker(symbol)
    try:
        news = ticker.news
        if not news:
            return []
        items = []
        for item in news[:5]:
            items.append({
                "title": item.get("content", {}).get("title") or item.get("title", ""),
                "publisher": (item.get("content", {}).get("provider", {}) or {}).get("displayName") or item.get("publisher", ""),
                "published": item.get("content", {}).get("pubDate") or str(item.get("providerPublishTime", "")),
                "url": (item.get("content", {}).get("canonicalUrl", {}) or {}).get("url") or item.get("link", ""),
            })
        return items
    except Exception as e:
        logger.warning("Yahoo Finance news fetch failed for %s: %s", symbol, e)
        return []


def _fetch_news_from_multiple_sources(symbol: str, max_total: int = 15) -> List[Dict[str, Any]]:
    all_news = []

    # 1. Yahoo Finance
    yf_news = _fetch_yf_news(symbol)
    logger.info(f"Yahoo Finance news: {len(yf_news)} items")
    if yf_news:
        all_news.extend(yf_news)

    # 2. Google News
    google_news = _fetch_google_news(symbol, max_items=8)
    logger.info(f"Google News: {len(google_news)} items")
    if google_news:
        all_news.extend(google_news)

    # 3. Moneycontrol
    mc_news = _fetch_moneycontrol_news(symbol, max_items=5)
    logger.info(f"Moneycontrol: {len(mc_news)} items")
    if mc_news:
        all_news.extend(mc_news)

    # 4. Economic Times
    et_news = _fetch_economic_times(symbol, max_items=5)
    logger.info(f"Economic Times: {len(et_news)} items")
    if et_news:
        all_news.extend(et_news)

    # 5. CNBC TV18
    cnbc_news = _fetch_cnbc_tv18(symbol, max_items=5)
    logger.info(f"CNBC TV18: {len(cnbc_news)} items")
    if cnbc_news:
        all_news.extend(cnbc_news)

    # Deduplicate
    seen = set()
    unique = []
    for item in all_news:
        key = item["title"].strip().lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)

    # Sort by published date (newest first)
    unique.sort(key=lambda x: x.get("published") or "", reverse=True)

    # Log final count
    logger.info(f"Total unique news after dedup: {len(unique)}")
    return unique[:max_total]


# ── Core event fetch ──────────────────────────────────────────────────────────
def _fetch_events(symbol: str, force: bool = False) -> dict:
    sym = _normalize(symbol)
    cache_key = f"{EVENT_CACHE_PREFIX}{sym}"

    if not force:
        cached = _redis_get(cache_key)
        if cached and cached.get("recent_news") and len(cached["recent_news"]) > 0:
            logger.info(f"Event cache hit for {sym} with {len(cached['recent_news'])} news")
            return cached
        elif cached:
            logger.info(f"Cache for {sym} has empty news; will fetch fresh")

    logger.info(f"=== Fetching fresh events for {sym} ===")
    ticker = yf.Ticker(sym)
    ticker._tz = "Asia/Kolkata"

    # Existing yfinance data (unchanged)
    next_earnings = None
    earnings_dates = _yf_call(lambda: ticker.get_earnings_dates(limit=1), "Earnings dates", sym)
    if earnings_dates is not None and not earnings_dates.empty:
        try:
            next_earnings = str(earnings_dates.index[0].date())
        except Exception:
            pass

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

    # ── Multi-source news ──
    recent_news = _fetch_news_from_multiple_sources(sym, max_total=15)

    # Earnings surprise
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

    bulk_deals = []
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
        ttl = EVENT_CACHE_TTL if recent_news else EMPTY_NEWS_CACHE_TTL
        if not recent_news:
            logger.info(f"No news for {sym}; caching with short TTL ({ttl}s)")
        _redis_set(cache_key, {**result, "cached": True}, ttl=ttl)
        _redis_set(fallback_key, result, ttl=EVENT_FALLBACK_TTL)
        logger.info(f"Finished fetching events for {sym}: {len(recent_news)} news items")
        return result

    stale = _redis_get(fallback_key)
    if stale:
        logger.info(f"Live fetch for {sym} empty; serving fallback")
        stale = {**stale, "cached": True, "stale": True}
        _redis_set(cache_key, stale, ttl=900)
        return stale

    return result


# ── Routes (unchanged) ──
@app.get("/")
def root():
    return {
        "service": "Stockky Event Tracker Service",
        "version": "0.4.1",
        "status": "running",
        "endpoints": {
            "/health": "GET",
            "/events/{symbol}": "GET full snapshot",
            "/events/{symbol}?force=true": "GET bypass cache",
            "/subscribe": "POST",
            "/subscriptions": "GET",
            "/check": "GET",
            "/symbols_with_events": "GET",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "event-tracker-service", "redis": bool(_redis)}


@app.get("/events/{symbol}")
def get_events(symbol: str, force: bool = False):
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
    state = _load_state()
    changes = []
    for i, symbol in enumerate(state["subscriptions"]):
        if i > 0:
            time.sleep(1)
        current = _fetch_events(symbol)
        previous = state["last_known"].get(symbol, {})
        diff_reasons = []

        if previous.get("next_earnings_date") != current.get("next_earnings_date"):
            diff_reasons.append(f"Earnings date: {previous.get('next_earnings_date')} → {current.get('next_earnings_date')}")
        prev_div = previous.get("last_dividend") or {}
        cur_div = current.get("last_dividend") or {}
        if prev_div.get("date") != cur_div.get("date") and cur_div.get("date"):
            diff_reasons.append(f"New dividend: ₹{cur_div.get('amount')} on {cur_div.get('date')}")
        prev_split = previous.get("last_split") or {}
        cur_split = current.get("last_split") or {}
        if prev_split.get("date") != cur_split.get("date") and cur_split.get("date"):
            diff_reasons.append(f"Stock split: {cur_split.get('ratio')}:1 on {cur_split.get('date')}")
        prev_keys = {(a.get("date","")+a.get("firm","")) for a in (previous.get("recent_analyst_actions") or [])}
        for action in (current.get("recent_analyst_actions") or []):
            key = action.get("date","") + action.get("firm","")
            if key not in prev_keys:
                diff_reasons.append(f"Analyst: {action.get('firm')} {action.get('action')} → {action.get('to_grade')}")
        prev_insider_keys = {(a.get("date","")+a.get("insider","")) for a in (previous.get("recent_insider_transactions") or [])}
        for txn in (current.get("recent_insider_transactions") or []):
            key = txn.get("date","") + txn.get("insider","")
            if key not in prev_insider_keys:
                diff_reasons.append(f"Insider {txn.get('transaction')}: {txn.get('insider')} — {txn.get('shares')} shares")
        prev_surprise = previous.get("earnings_surprise") or {}
        cur_surprise = current.get("earnings_surprise") or {}
        if prev_surprise.get("surprise_pct") != cur_surprise.get("surprise_pct"):
            diff_reasons.append(f"Earnings surprise: {cur_surprise.get('surprise_pct')}%")
        prev_bulk = previous.get("bulk_deals") or []
        cur_bulk = current.get("bulk_deals") or []
        if len(cur_bulk) != len(prev_bulk):
            diff_reasons.append("Bulk/Block deal detected")
        if diff_reasons:
            changes.append({"symbol": symbol, "changes": diff_reasons, "current": current})
        state["last_known"][symbol] = current
    _save_state(state)
    return {"checked": len(state["subscriptions"]), "changes": changes, "checked_at": datetime.utcnow().isoformat()}


@app.get("/symbols_with_events")
def symbols_with_events(days_ahead: int = 7):
    cached = _redis_get(EVENTS_LIST_CACHE_KEY)
    if cached and isinstance(cached, list):
        return {"symbols": cached}
    state = _load_state()
    subscriptions = state.get("subscriptions", [])
    if not subscriptions:
        _redis_set(EVENTS_LIST_CACHE_KEY, [], ttl=EVENTS_LIST_CACHE_TTL)
        return {"symbols": []}
    now = datetime.utcnow()
    cutoff = now + timedelta(days=days_ahead)
    result_symbols = []
    for symbol in subscriptions:
        cache_key = f"{EVENT_CACHE_PREFIX}{symbol}"
        cached_events = _redis_get(cache_key)
        if not cached_events:
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
        last_div = cached_events.get("last_dividend")
        if last_div and last_div.get("date"):
            try:
                dt = datetime.fromisoformat(last_div["date"])
                if now <= dt <= cutoff:
                    result_symbols.append(symbol)
                    continue
            except (ValueError, TypeError):
                pass
        last_split = cached_events.get("last_split")
        if last_split and last_split.get("date"):
            try:
                dt = datetime.fromisoformat(last_split["date"])
                if now <= dt <= cutoff:
                    result_symbols.append(symbol)
                    continue
            except (ValueError, TypeError):
                pass
    result_symbols = sorted(set(result_symbols))
    _redis_set(EVENTS_LIST_CACHE_KEY, result_symbols, ttl=EVENTS_LIST_CACHE_TTL)
    return {"symbols": result_symbols}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8006))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)