"""
API Gateway
------------
Single entry point the React frontend talks to.
Watchlist is now persisted in Upstash Redis — survives restarts.
"""
import os
import json
import time
import asyncio
import logging
from typing import List

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from upstash_redis import Redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api-gateway")

# ---- Live Render URLs (default) ----
DECISION_URL = os.getenv("DECISION_URL", "https://decision-engine-service-0hg6.onrender.com")
NOTIFICATION_URL = os.getenv("NOTIFICATION_URL", "https://notification-service-36py.onrender.com")

# Optional downstream services for /system/health (wake‑up checks)
MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://market-data-service.onrender.com")
TECHNICAL_URL = os.getenv("TECHNICAL_URL", "https://technical-analysis-service-zhnc.onrender.com")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", "https://fundamental-analysis-service.onrender.com")
NEWS_URL = os.getenv("NEWS_URL", "https://news-intelligence-service.onrender.com")
EVENT_URL = os.getenv("EVENT_URL", "https://event-tracker-service-m1lw.onrender.com")
PREDICTION_URL = os.getenv("PREDICTION_URL", "https://prediction-service-wowb.onrender.com")

# Service definitions for system health
SYSTEM_SERVICES = {
    "market-data": {"url": MARKET_DATA_URL, "required": True},
    "technical-analysis": {"url": TECHNICAL_URL, "required": True},
    "fundamental-analysis": {"url": FUNDAMENTAL_URL, "required": True},
    "decision-engine": {"url": DECISION_URL, "required": True},
    "news-intelligence": {"url": NEWS_URL, "required": False},
    "event-tracker": {"url": EVENT_URL, "required": False},
    "prediction": {"url": PREDICTION_URL, "required": False},
    "notification": {"url": NOTIFICATION_URL, "required": False},
}

app = FastAPI(title="Stockky API Gateway", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DEFAULT_WATCHLIST = [
    "TCS", "INFY", "HDFCBANK", "ICICIBANK", "RELIANCE", "HCLTECH",
    "WIPRO", "COFORGE", "ANGELONE", "ADANIPOWER", "BEL", "HAL",
    "TATAMOTORS", "SBIN", "AXISBANK", "KOTAKBANK", "LT", "MARUTI",
]

WATCHLIST_KEY = "stockky:watchlist"

_redis = None
try:
    _redis = Redis(
        url=os.getenv("UPSTASH_REDIS_REST_URL"),
        token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
    )
    _redis.ping()
    logger.info("Connected to Upstash Redis")
except Exception as e:
    logger.warning("Redis unavailable — watchlist will not persist: %s", e)


def _load_watchlist() -> List[str]:
    if _redis:
        try:
            val = _redis.get(WATCHLIST_KEY)
            if val:
                return json.loads(val)
        except Exception as e:
            logger.warning("Failed to load watchlist from Redis: %s", e)
    return list(DEFAULT_WATCHLIST)


def _save_watchlist(symbols: List[str]):
    if _redis:
        try:
            _redis.set(WATCHLIST_KEY, json.dumps(symbols))
        except Exception as e:
            logger.warning("Failed to persist watchlist: %s", e)


class WatchlistUpdate(BaseModel):
    symbols: List[str]


@app.get("/")
def root():
    return {
        "service": "Stockky API Gateway",
        "version": "0.2.0",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/system/health": "GET – health of all downstream services",
            "/watchlist": "GET/POST – manage watchlist",
            "/watchlist/add": "POST – add symbols",
            "/watchlist/{symbol}": "DELETE – remove symbol",
            "/stock/{symbol}": "GET – get decision for a symbol",
            "/scan": "GET – scan watchlist",
            "/notifications/health": "GET – notification service health",
            "/notifications/config": "GET/POST – get/update notification config",
            "/notifications/config/{channel}": "DELETE – clear a channel",
            "/notifications/test": "POST – test notifications",
            "/docs": "Swagger UI documentation",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "api-gateway", "redis": bool(_redis)}


@app.get("/system/health")
async def system_health():
    """Pings every downstream service AT THE SAME TIME. This matters on free
    tiers specifically: a sleeping Render service can take 30-60s to wake on
    its first request. Waking them one-by-one in sequence (as a normal
    request through decision-engine-service does) can stack up past
    Render's own proxy timeout and surface as an opaque 502. Hitting them
    all concurrently from here means the total wait is roughly the SLOWEST
    single cold start, not the sum of all of them — and the frontend can
    show live per-service status while it waits."""

    async def check(name: str, url: str, required: bool):
        if not url:
            return name, {"ok": False, "required": required, "status": "not_configured"}
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=70) as client:
                resp = await client.get(f"{url.rstrip('/')}/health")
            elapsed = round(time.monotonic() - start, 1)
            if resp.status_code == 200:
                return name, {"ok": True, "required": required, "status": "up", "seconds": elapsed}
            return name, {
                "ok": False,
                "required": required,
                "status": f"http_{resp.status_code}",
                "seconds": elapsed,
            }
        except httpx.HTTPError as e:
            elapsed = round(time.monotonic() - start, 1)
            return name, {
                "ok": False,
                "required": required,
                "status": "unreachable",
                "seconds": elapsed,
                "error": str(e)[:200],
            }

    results = await asyncio.gather(
        *(check(name, cfg["url"], cfg["required"]) for name, cfg in SYSTEM_SERVICES.items())
    )
    services = {"api-gateway": {"ok": True, "required": True, "status": "up", "seconds": 0}}
    services.update(dict(results))

    required_ok = all(v["ok"] for v in services.values() if v["required"])
    all_ok = all(v["ok"] for v in services.values())

    return {"required_ok": required_ok, "all_ok": all_ok, "services": services}


@app.get("/watchlist")
def get_watchlist():
    return {"symbols": _load_watchlist()}


@app.post("/watchlist")
def set_watchlist(update: WatchlistUpdate):
    symbols = [s.strip().upper() for s in update.symbols]
    _save_watchlist(symbols)
    return {"symbols": symbols}


@app.post("/watchlist/add")
def add_to_watchlist(update: WatchlistUpdate):
    current = set(_load_watchlist())
    for s in update.symbols:
        current.add(s.strip().upper())
    symbols = sorted(current)
    _save_watchlist(symbols)
    return {"symbols": symbols}


@app.delete("/watchlist/{symbol}")
def remove_from_watchlist(symbol: str):
    current = _load_watchlist()
    updated = [s for s in current if s != symbol.upper()]
    _save_watchlist(updated)
    return {"symbols": updated}


@app.get("/stock/{symbol}")
def get_stock_decision(symbol: str, already_owned: bool = False):
    try:
        resp = httpx.get(
            f"{DECISION_URL}/decide/{symbol}",
            params={"already_owned": already_owned},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Decision engine unreachable: {e}")


@app.get("/scan")
def run_scan():
    watchlist = _load_watchlist()
    results = []
    errors = []

    with httpx.Client(timeout=30) as client:
        for symbol in watchlist:
            try:
                resp = client.get(f"{DECISION_URL}/decide/{symbol}")
                resp.raise_for_status()
                results.append(resp.json())
            except httpx.HTTPError as e:
                logger.warning("Scan skipped %s: %s", symbol, e)
                errors.append({"symbol": symbol, "error": str(e)})

    actionable = [r for r in results if r["decision"] in ("BUY NOW", "PREPARE TO BUY")]
    actionable.sort(key=lambda r: r["combined_score"], reverse=True)
    top_picks = actionable[:3]

    return {
        "scanned": len(results),
        "watchlist_size": len(watchlist),
        "recommendations": top_picks,
        "verdict": (
            "DO NOT BUY ANY STOCK TODAY" if not top_picks
            else f"{len(top_picks)} opportunity(ies) found"
        ),
        "all_results": results,
        "errors": errors,
    }


class NotificationChannelUpdate(BaseModel):
    discord_webhook_url: str | None = None
    slack_webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    enabled: dict | None = None


@app.get("/notifications/health")
def notifications_health():
    """Lets the frontend know whether the notification-service is even
    reachable, separately from whether any channel is configured."""
    try:
        resp = httpx.get(f"{NOTIFICATION_URL}/health", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")


@app.get("/notifications/config")
def get_notification_config():
    try:
        resp = httpx.get(f"{NOTIFICATION_URL}/config", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")


@app.post("/notifications/config")
def set_notification_config(update: NotificationChannelUpdate):
    try:
        resp = httpx.post(
            f"{NOTIFICATION_URL}/config",
            json=update.model_dump(exclude_none=True),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")


@app.delete("/notifications/config/{channel}")
def delete_notification_channel(channel: str):
    try:
        resp = httpx.delete(f"{NOTIFICATION_URL}/config/{channel}", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")


@app.post("/notifications/test")
def test_notification_channels():
    try:
        resp = httpx.post(f"{NOTIFICATION_URL}/test", timeout=15)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Notification service unreachable: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)