"""
Scheduler Service
-------------------
Single responsibility: trigger the API Gateway's /scan on a timer during
Indian market hours, detect decision changes worth notifying about, poll
the Event Tracker for material corporate events, and write the end-of-day
report. This service now runs as a web service (with FastAPI) so it can
bind to a port — the scheduler runs in a background thread.

Market hours (NSE): 09:15-15:30 IST. Per the spec, scans run from 1 hour
before open to 1 hour after close, every 30-60 minutes.

Notification rule (per spec — "no unnecessary notifications"): only notify
when a symbol's decision newly becomes BUY NOW, or flips from a BUY-family
decision to SELL. Every other scan is silent even if it ran successfully.

Keep-alive: pings all Render services every 14 minutes so free-tier
instances never spin down during the day.
"""
import os
import json
import logging
import threading
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler-service")

API_GATEWAY_URL   = os.getenv("API_GATEWAY_URL",   "https://api-gateway-wizr.onrender.com")
EVENT_TRACKER_URL = os.getenv("EVENT_TRACKER_URL", "https://event-tracker-service-m1lw.onrender.com")
NOTIFICATION_URL  = os.getenv("NOTIFICATION_URL",  "https://notification-service-36py.onrender.com")
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
REPORTS_DIR  = os.getenv("REPORTS_DIR", "/tmp/reports")
STATE_PATH   = os.getenv("SCHEDULER_STATE_PATH", "/tmp/reports/last_decisions.json")
IST = ZoneInfo("Asia/Kolkata")

SCAN_WINDOW_START = dtime(8, 15)   # 1hr before market open (09:15 IST)
SCAN_WINDOW_END   = dtime(16, 30)  # 1hr after market close (15:30 IST)

os.makedirs(REPORTS_DIR, exist_ok=True)

BUY_FAMILY = {"BUY NOW", "PREPARE TO BUY", "HOLD"}

# All services that need to stay warm on Render free tier
KEEPALIVE_ENDPOINTS = [
    "https://stockky-market-data.onrender.com/health",
    "https://technical-analysis-service-zhnc.onrender.com/health",
    "https://api-gateway-wizr.onrender.com/health",
    "https://decision-engine-service-0hg6.onrender.com/health",
    "https://event-tracker-service-m1lw.onrender.com/health",
    "https://prediction-service-wowb.onrender.com/health",
    "https://notification-service-36py.onrender.com/health",
    "https://fundamental-analysis-service.onrender.com/health",
    "https://news-intelligence-service.onrender.com/health",
]


# ---------- FastAPI app ----------
app = FastAPI(title="Stockky Scheduler Service", version="1.0.0")

@app.get("/")
def root():
    return {
        "service": "Stockky Scheduler Service",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
        },
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "scheduler-service"}

# ---------- Job functions (unchanged) ----------
def within_market_window(now: datetime) -> bool:
    if now.weekday() >= 5:  # Sat/Sun — NSE closed
        return False
    return SCAN_WINDOW_START <= now.time() <= SCAN_WINDOW_END


def _load_last_decisions() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def _save_last_decisions(decisions: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(decisions, f, indent=2)


def _notify(title: str, message: str):
    try:
        httpx.post(
            f"{NOTIFICATION_URL}/notify",
            json={"title": title, "message": message},
            timeout=10,
        )
    except httpx.HTTPError as e:
        logger.warning("Notification dispatch failed (non-fatal): %s", e)


def _check_decision_changes(all_results: list):
    """Compare this scan's decisions against the previous scan's and notify
    only on the two transitions the spec cares about."""
    previous = _load_last_decisions()
    current  = {r["symbol"]: r["decision"] for r in all_results}

    for symbol, decision in current.items():
        prev_decision = previous.get(symbol)

        if decision == "BUY NOW" and prev_decision != "BUY NOW":
            _notify(
                f"🟢 New BUY NOW: {symbol}",
                f"{symbol} just became a BUY NOW opportunity. Check Stockky for entry/target/stop-loss.",
            )
        elif decision == "SELL" and prev_decision in BUY_FAMILY:
            _notify(
                f"🔴 {symbol} flipped to SELL",
                f"{symbol} moved from {prev_decision} to SELL. Review your position.",
            )

    _save_last_decisions(current)


def run_keepalive_job():
    """Ping every service health endpoint every 14 minutes.
    Render free tier spins down after 15 minutes of inactivity —
    this keeps all services warm so the first real request never times out."""
    now = datetime.now(IST)
    results = []
    for url in KEEPALIVE_ENDPOINTS:
        try:
            resp = httpx.get(url, timeout=20)
            results.append(f"✓ {url.split('/')[2].split('.')[0]} ({resp.status_code})")
        except httpx.HTTPError as e:
            results.append(f"✗ {url.split('/')[2].split('.')[0]} ({e})")
    logger.info("Keep-alive ping at %s IST: %s", now.strftime("%H:%M"), " | ".join(results))


def run_scan_job():
    now = datetime.now(IST)
    if not within_market_window(now):
        logger.info("Outside market scan window (%s IST) — skipping scan.", now.strftime("%H:%M"))
        return

    try:
        resp = httpx.get(f"{API_GATEWAY_URL}/scan", timeout=90)
        resp.raise_for_status()
        result = resp.json()
        logger.info("Scan complete: %s", result.get("verdict"))
        _check_decision_changes(result.get("all_results", []))
    except httpx.HTTPError as e:
        logger.error("Scan failed: %s", e)


def run_event_check_job():
    """Poll the Event Tracker for anything that changed on subscribed
    symbols (new dividend, split, or updated earnings date) and notify."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return
    try:
        resp = httpx.get(f"{EVENT_TRACKER_URL}/check", timeout=60)
        resp.raise_for_status()
        result = resp.json()
        for change in result.get("changes", []):
            _notify(
                f"📅 Event update: {change['symbol']}",
                "\n".join(change["changes"]),
            )
        if result.get("changes"):
            logger.info("Event changes detected: %d", len(result["changes"]))
    except httpx.HTTPError as e:
        logger.warning("Event check failed (non-fatal): %s", e)


def sync_event_subscriptions():
    """Make sure the Event Tracker is watching the same symbols as the
    scan watchlist. Runs once at startup."""
    try:
        wl = httpx.get(f"{API_GATEWAY_URL}/watchlist", timeout=15).json()
        httpx.post(
            f"{EVENT_TRACKER_URL}/subscribe",
            json={"symbols": wl["symbols"]},
            timeout=15,
        )
        logger.info("Event Tracker subscriptions synced: %s", wl["symbols"])
    except httpx.HTTPError as e:
        logger.warning("Could not sync event subscriptions at startup (non-fatal): %s", e)


def run_end_of_day_report():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return

    try:
        resp = httpx.get(f"{API_GATEWAY_URL}/scan", timeout=90)
        resp.raise_for_status()
        scan_result = resp.json()
    except httpx.HTTPError as e:
        logger.error("End-of-day report failed to fetch scan: %s", e)
        return

    report = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(),
        "market_summary": scan_result.get("verdict"),
        "buy_recommendations": scan_result.get("recommendations", []),
        "stocks_avoided": [
            r["symbol"] for r in scan_result.get("all_results", [])
            if r["decision"] == "DO NOT BUY"
        ],
        "scanned_count": scan_result.get("scanned"),
    }

    path = os.path.join(REPORTS_DIR, f"{report['date']}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    logger.info("End-of-day report saved: %s", path)
    _notify(
        "📊 End-of-day report ready",
        f"{report['market_summary']} — {report['scanned_count']} stocks scanned.",
    )


# ---------- Scheduler startup in background thread ----------
def start_scheduler():
    sync_event_subscriptions()
    run_keepalive_job()  # warm up at startup

    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(run_keepalive_job, "interval", minutes=14, id="keepalive")
    scheduler.add_job(run_scan_job, "interval", minutes=SCAN_INTERVAL_MINUTES, id="market_scan")
    scheduler.add_job(run_event_check_job, "interval", hours=2, id="event_check")
    scheduler.add_job(run_end_of_day_report, "cron", hour=16, minute=0, day_of_week="mon-fri", id="eod_report")

    scheduler.start()
    logger.info(
        "Scheduler started. Keep-alive every 14min. Market scan every %s min during IST market hours.",
        SCAN_INTERVAL_MINUTES,
    )
    # Keep the thread alive (BackgroundScheduler runs in its own threads, so we just wait)
    # Since this runs in a daemon thread, we need to prevent it from exiting immediately.
    # We'll use an infinite loop or a lock. A simple join on a dummy event works.
    import threading
    event = threading.Event()
    event.wait()  # Wait forever; the thread will be daemon so it exits when main exits.


# ---------- Entry point ----------
if __name__ == "__main__":
    # Start the scheduler in a daemon thread so it doesn't block uvicorn
    scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
    scheduler_thread.start()

    # Run the web server
    port = int(os.environ.get("PORT", 8009))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)