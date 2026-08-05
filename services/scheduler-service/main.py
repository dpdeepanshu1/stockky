"""
Scheduler Service
-------------------
Single responsibility: trigger the API Gateway's /scan on a timer during
Indian market hours, detect decision changes worth notifying about, poll
the Event Tracker for material corporate events, and write the end-of-day
report. This service has no HTTP API of its own — it's a background worker
container.

Market hours (NSE): 09:15-15:30 IST. Per the spec, scans run from 1 hour
before open to 1 hour after close, every 30-60 minutes.

Notification rule (per spec — "no unnecessary notifications"): only notify
when a symbol's decision newly becomes BUY NOW, or flips from a BUY-family
decision to SELL. Every other scan is silent even if it ran successfully.
"""
import os
import json
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler-service")

API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://api-gateway:8000")
EVENT_TRACKER_URL = os.getenv("EVENT_TRACKER_URL", "http://event-tracker-service:8006")
NOTIFICATION_URL = os.getenv("NOTIFICATION_URL", "http://notification-service:8008")
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
REPORTS_DIR = os.getenv("REPORTS_DIR", "/app/reports")
STATE_PATH = os.getenv("SCHEDULER_STATE_PATH", "/app/reports/last_decisions.json")
IST = ZoneInfo("Asia/Kolkata")

SCAN_WINDOW_START = dtime(8, 15)   # 1hr before market open (09:15 IST)
SCAN_WINDOW_END = dtime(16, 30)    # 1hr after market close (15:30 IST)

os.makedirs(REPORTS_DIR, exist_ok=True)

BUY_FAMILY = {"BUY NOW", "PREPARE TO BUY", "HOLD"}


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
        httpx.post(f"{NOTIFICATION_URL}/notify", json={"title": title, "message": message}, timeout=10)
    except httpx.HTTPError as e:
        logger.warning("Notification dispatch failed (non-fatal): %s", e)


def _check_decision_changes(all_results: list):
    """Compare this scan's decisions against the previous scan's and notify
    only on the two transitions the spec cares about."""
    previous = _load_last_decisions()
    current = {r["symbol"]: r["decision"] for r in all_results}

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
        httpx.post(f"{EVENT_TRACKER_URL}/subscribe", json={"symbols": wl["symbols"]}, timeout=15)
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
    _notify("📊 End-of-day report ready", f"{report['market_summary']} — {report['scanned_count']} stocks scanned.")


if __name__ == "__main__":
    sync_event_subscriptions()

    scheduler = BlockingScheduler(timezone=IST)
    scheduler.add_job(run_scan_job, "interval", minutes=SCAN_INTERVAL_MINUTES, id="market_scan")
    scheduler.add_job(run_event_check_job, "interval", hours=2, id="event_check")
    scheduler.add_job(run_end_of_day_report, "cron", hour=16, minute=0, day_of_week="mon-fri", id="eod_report")
    logger.info("Scheduler started. Scanning every %s minutes during market hours (IST).", SCAN_INTERVAL_MINUTES)
    scheduler.start()
