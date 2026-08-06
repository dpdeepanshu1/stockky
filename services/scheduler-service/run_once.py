"""
Scheduler — single-shot mode, driven by GitHub Actions cron instead of a
long-running container. Render's free tier has no Background Worker type
and would put a free Web Service to sleep after ~15 min idle anyway, which
would kill an in-process scheduler loop — a GitHub Actions cron job has no
such sleep problem and costs nothing for a public repo.

Because each run is a brand-new, throwaway GitHub Actions runner, the
"what changed since last scan" state that used to live in a local JSON
file now lives in the same Upstash Redis instance the rest of the app
already uses, so it survives between separate runs.

Each invocation does one "tick":
  1. Sync Event Tracker subscriptions to the current watchlist (cheap,
     idempotent — safe to do every run).
  2. If we're inside the market scan window, run /scan and notify only on
     the two transitions that matter: a symbol newly becoming BUY NOW, or
     flipping from a BUY-family decision to SELL.
  3. Check the Event Tracker for material corporate-action changes.
  4. Once, at/after market close, send the end-of-day summary (a Redis
     flag prevents sending it more than once per day even though this
     script runs many times that day).
"""
import os
import json
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import httpx
from upstash_redis import Redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler-once")

API_GATEWAY_URL = os.environ["API_GATEWAY_URL"]
EVENT_TRACKER_URL = os.environ["EVENT_TRACKER_URL"]
NOTIFICATION_URL = os.environ["NOTIFICATION_URL"]
IST = ZoneInfo("Asia/Kolkata")

# 1hr before open (09:15 IST) to 1hr after close (15:30 IST), per spec.
SCAN_WINDOW_START = dtime(8, 15)
SCAN_WINDOW_END = dtime(16, 30)
EOD_HOUR = 16  # send the end-of-day report at/after 16:00 IST

STATE_KEY = "stockky:scheduler:last_decisions"
EOD_KEY_PREFIX = "stockky:scheduler:eod:"  # + date, so EOD only fires once/day

_redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
)

BUY_FAMILY = {"BUY NOW", "PREPARE TO BUY", "HOLD"}


def within_market_window(now: datetime) -> bool:
    if now.weekday() >= 5:  # Sat/Sun — NSE closed
        return False
    return SCAN_WINDOW_START <= now.time() <= SCAN_WINDOW_END


def _notify(title: str, message: str):
    try:
        httpx.post(f"{NOTIFICATION_URL}/notify", json={"title": title, "message": message}, timeout=10)
    except httpx.HTTPError as e:
        logger.warning("Notification dispatch failed (non-fatal): %s", e)


def _load_last_decisions() -> dict:
    try:
        val = _redis.get(STATE_KEY)
        return json.loads(val) if val else {}
    except Exception as e:
        logger.warning("Could not load previous decisions from Redis: %s", e)
        return {}


def _save_last_decisions(decisions: dict):
    try:
        _redis.set(STATE_KEY, json.dumps(decisions))
    except Exception as e:
        logger.warning("Could not persist decisions to Redis: %s", e)


def check_decision_changes(all_results: list):
    previous = _load_last_decisions()
    current = {r["symbol"]: r["decision"] for r in all_results}

    for symbol, decision in current.items():
        prev_decision = previous.get(symbol)
        if decision == "BUY NOW" and prev_decision != "BUY NOW":
            _notify(
                f"\U0001F7E2 New BUY NOW: {symbol}",
                f"{symbol} just became a BUY NOW opportunity. Check Stockky for entry/target/stop-loss.",
            )
        elif decision == "SELL" and prev_decision in BUY_FAMILY:
            _notify(
                f"\U0001F534 {symbol} flipped to SELL",
                f"{symbol} moved from {prev_decision} to SELL. Review your position.",
            )

    _save_last_decisions(current)


def sync_event_subscriptions():
    try:
        wl = httpx.get(f"{API_GATEWAY_URL}/watchlist", timeout=15).json()
        httpx.post(f"{EVENT_TRACKER_URL}/subscribe", json={"symbols": wl["symbols"]}, timeout=15)
        logger.info("Event Tracker subscriptions synced: %s", wl["symbols"])
    except httpx.HTTPError as e:
        logger.warning("Could not sync event subscriptions (non-fatal): %s", e)


def run_event_check():
    try:
        resp = httpx.get(f"{EVENT_TRACKER_URL}/check", timeout=60)
        resp.raise_for_status()
        result = resp.json()
        for change in result.get("changes", []):
            _notify(f"\U0001F4C5 Event update: {change['symbol']}", "\n".join(change["changes"]))
        if result.get("changes"):
            logger.info("Event changes detected: %d", len(result["changes"]))
    except httpx.HTTPError as e:
        logger.warning("Event check failed (non-fatal): %s", e)


def run_scan_and_diff():
    try:
        resp = httpx.get(f"{API_GATEWAY_URL}/scan", timeout=90)
        resp.raise_for_status()
        result = resp.json()
        logger.info("Scan complete: %s", result.get("verdict"))
        check_decision_changes(result.get("all_results", []))
        return result
    except httpx.HTTPError as e:
        logger.error("Scan failed: %s", e)
        return None


def maybe_run_end_of_day(now: datetime, scan_result):
    if now.weekday() >= 5 or now.hour < EOD_HOUR:
        return
    date_key = EOD_KEY_PREFIX + now.strftime("%Y-%m-%d")
    try:
        if _redis.get(date_key):
            return  # already sent today
    except Exception:
        pass

    if scan_result is None:
        try:
            resp = httpx.get(f"{API_GATEWAY_URL}/scan", timeout=90)
            resp.raise_for_status()
            scan_result = resp.json()
        except httpx.HTTPError as e:
            logger.error("End-of-day report failed to fetch scan: %s", e)
            return

    verdict = scan_result.get("verdict")
    scanned = scan_result.get("scanned")
    _notify("\U0001F4CA End-of-day report ready", f"{verdict} — {scanned} stocks scanned.")
    try:
        _redis.set(date_key, "1")
    except Exception as e:
        logger.warning("Could not mark EOD report as sent: %s", e)


def main():
    now = datetime.now(IST)
    logger.info("Scheduler tick at %s IST", now.strftime("%Y-%m-%d %H:%M"))

    sync_event_subscriptions()

    scan_result = None
    if within_market_window(now):
        scan_result = run_scan_and_diff()
    else:
        logger.info("Outside market scan window (%s IST) — skipping scan.", now.strftime("%H:%M"))

    run_event_check()
    maybe_run_end_of_day(now, scan_result)


if __name__ == "__main__":
    main()