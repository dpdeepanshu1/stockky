"""
Scheduler — single-shot mode, driven by GitHub Actions cron.

Enhanced with timed notifications:
  - 08:15 IST: "Market opens in 1 hour"
  - 09:15 IST: "Market is open now"
  - 30-min scans (08:30..15:30) with top picks
  - 15:30 IST: Market close summary
  - 16:30 IST: "Going to sleep" + preview for tomorrow
Uses Redis to track sent messages (so each event fires only once per day).
"""
import os
import json
import logging
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from typing import List, Dict, Any

import httpx
from upstash_redis import Redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler-once")

API_GATEWAY_URL = os.environ["API_GATEWAY_URL"]
EVENT_TRACKER_URL = os.environ["EVENT_TRACKER_URL"]
NOTIFICATION_URL = os.environ["NOTIFICATION_URL"]
IST = ZoneInfo("Asia/Kolkata")

# Market hours (IST)
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
SCAN_START = dtime(8, 30)   # first scan
SCAN_END = dtime(15, 30)    # last scan (at close)
OPEN_ANNOUNCE_TIME = dtime(8, 15)
CLOSE_SUMMARY_TIME = dtime(15, 30)
SLEEP_TIME = dtime(16, 30)

# Redis keys
STATE_KEY = "stockky:scheduler:last_decisions"
EOD_KEY_PREFIX = "stockky:scheduler:eod:"        # + date
OPEN_MSG_KEY = "stockky:scheduler:open_msg:"     # + date (sent open-in-1h)
OPEN_NOW_KEY = "stockky:scheduler:open_now:"     # + date (sent market-open)
CLOSE_MSG_KEY = "stockky:scheduler:close_msg:"   # + date (sent close summary)
SLEEP_MSG_KEY = "stockky:scheduler:sleep_msg:"   # + date (sent sleep)
DAILY_PICKS_KEY = "stockky:scheduler:picks:"     # + date (store top picks of the day)

# NSE holidays 2026 (static; can be extended or fetched from API)
HOLIDAYS_2026 = [
    "2026-01-26",  # Republic Day
    "2026-03-02",  # Holi
    "2026-03-31",  # Eid ul-Fitr
    "2026-04-02",  # Ram Navami
    "2026-04-10",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day / Labour Day
    "2026-08-15",  # Independence Day
    "2026-10-02",  # Gandhi Jayanti
    "2026-10-22",  # Dussehra
    "2026-11-14",  # Diwali
    "2026-11-15",  # Diwali (Balipratipada)
    "2026-12-25",  # Christmas
]

_redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
)

BUY_FAMILY = {"BUY NOW", "PREPARE TO BUY", "HOLD"}


def is_holiday(today: datetime) -> bool:
    date_str = today.strftime("%Y-%m-%d")
    if today.weekday() >= 5:  # weekend
        return True
    if date_str in HOLIDAYS_2026:
        return True
    return False


def _notify(title: str, message: str, channel: str = "telegram"):
    """Send notification via the notification service."""
    try:
        payload = {"title": title, "message": message, "channel": channel}
        httpx.post(f"{NOTIFICATION_URL}/notify", json=payload, timeout=10)
        logger.info("Notification sent: %s", title)
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


def run_scan_and_diff(timeout: int = 120) -> Dict[str, Any]:
    """Fetch scan results, notify on decision changes, and return the scan result."""
    try:
        resp = httpx.get(f"{API_GATEWAY_URL}/scan", timeout=timeout)
        resp.raise_for_status()
        result = resp.json()
        logger.info("Scan complete: %s", result.get("verdict"))
        check_decision_changes(result.get("all_results", []))
        return result
    except httpx.HTTPError as e:
        logger.error("Scan failed: %s", e)
        return {}


def format_stock_picks(picks: List[Dict]) -> str:
    """Format top picks into a readable message."""
    if not picks:
        return "No actionable BUY NOW / PREPARE TO BUY stocks at the moment."

    lines = ["🏆 *Top Picks:*"]
    for i, p in enumerate(picks[:5], 1):
        decision = p.get("decision", "UNKNOWN")
        sym = p.get("symbol", "?")
        score = p.get("combined_score", 0)
        entry = p.get("entry_range", {})
        target = p.get("target", 0)
        stop = p.get("stop_loss", 0)
        lines.append(f"{i}. *{sym}* – {decision} (Score: {score})")
        lines.append(f"   Entry: {entry.get('low')}–{entry.get('high')} | Target: {target} | Stop: {stop}")
    return "\n".join(lines)


def store_daily_picks(date_str: str, picks: List[Dict]):
    """Store the day's top picks in Redis for the summary."""
    key = DAILY_PICKS_KEY + date_str
    # We'll keep a list of picks (append new ones) but cap at 20 to avoid bloat.
    existing = _redis.get(key)
    if existing:
        try:
            existing_picks = json.loads(existing)
        except:
            existing_picks = []
    else:
        existing_picks = []

    # Merge: if a symbol already exists, update it; else append
    symbols = {p["symbol"]: p for p in existing_picks}
    for p in picks:
        symbols[p["symbol"]] = p
    new_list = list(symbols.values())
    # Sort by score descending, keep top 20
    new_list.sort(key=lambda x: x.get("combined_score", 0), reverse=True)
    if len(new_list) > 20:
        new_list = new_list[:20]
    _redis.set(key, json.dumps(new_list, default=str))


def get_daily_picks(date_str: str) -> List[Dict]:
    key = DAILY_PICKS_KEY + date_str
    data = _redis.get(key)
    if data:
        try:
            return json.loads(data)
        except:
            return []
    return []


def send_market_open_announcement():
    """Send 'Market opens in 1 hour' message (08:15)."""
    _notify("\U0001F55B Market opens in 1 hour", "The market will open at 09:15 IST. Get ready!")


def send_market_open_now():
    """Send 'Market is open now' message (09:15)."""
    _notify("\U0001F7E2 Market is now open", "Trading has started. Let's find opportunities!")


def send_scan_picks(picks: List[Dict]):
    """Send the top picks from the latest scan."""
    if not picks:
        _notify("\U0001F6AB No buy signals", "No actionable BUY NOW / PREPARE TO BUY stocks at the moment.")
    else:
        msg = format_stock_picks(picks)
        _notify("\U0001F4C8 Market Scan Update", msg)


def send_close_summary(date_str: str):
    """Send end-of-day summary: best picks of the day."""
    picks = get_daily_picks(date_str)
    if not picks:
        _notify("\U0001F4CA End of Day – No picks today", "No strong buy opportunities were found today.")
        return

    best = picks[:3]
    lines = ["📊 *End-of-Day Summary – Best picks of the day*"]
    for i, p in enumerate(best, 1):
        sym = p.get("symbol", "?")
        decision = p.get("decision", "UNKNOWN")
        score = p.get("combined_score", 0)
        entry = p.get("entry_range", {})
        target = p.get("target", 0)
        stop = p.get("stop_loss", 0)
        lines.append(f"{i}. *{sym}* – {decision} (Score: {score})")
        lines.append(f"   Entry: {entry.get('low')}–{entry.get('high')} | Target: {target} | Stop: {stop}")
    msg = "\n".join(lines)
    _notify("\U0001F4CA End-of-Day Summary", msg)


def send_sleep_message():
    """Send 'Going to sleep' message with preview for tomorrow."""
    # For preview, we could do an after-hours scan or just send a generic message.
    _notify("\U0001F634 Going to sleep", "Good night! I'll be back tomorrow before market open. Preview for tomorrow: Keep an eye on global cues and any after-market news.")


def main():
    now = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")
    time_now = now.time()

    # 1. Check holiday
    if is_holiday(now):
        logger.info("Market holiday – skipping all activity.")
        return

    # 2. Sync event subscriptions (always do this)
    sync_event_subscriptions()

    # 3. Determine which events to fire (using Redis flags)

    # Open announcement (08:15)
    if time_now == OPEN_ANNOUNCE_TIME:
        if not _redis.get(OPEN_MSG_KEY + today_str):
            send_market_open_announcement()
            _redis.set(OPEN_MSG_KEY + today_str, "1", ex=86400)  # expire after 1 day

    # Market open (09:15)
    if time_now == MARKET_OPEN:
        if not _redis.get(OPEN_NOW_KEY + today_str):
            send_market_open_now()
            _redis.set(OPEN_NOW_KEY + today_str, "1", ex=86400)

    # Regular scans: run at every 30-min interval from 08:30 to 15:30 inclusive
    # We'll run if minute is 0 or 30 and time between SCAN_START and SCAN_END (inclusive).
    if (time_now.minute in (0, 30)) and (SCAN_START <= time_now <= SCAN_END):
        logger.info("Running scheduled scan at %s", time_now.strftime("%H:%M"))
        scan_result = run_scan_and_diff()
        picks = scan_result.get("recommendations", [])  # top 5 picks from scan
        if picks:
            store_daily_picks(today_str, picks)
            send_scan_picks(picks)
        else:
            # Still send a message about no buy signals
            send_scan_picks([])

        # Also check events (maybe we want this only when scanning)
        run_event_check()

    # Market close summary (15:30)
    if time_now == CLOSE_SUMMARY_TIME:
        if not _redis.get(CLOSE_MSG_KEY + today_str):
            send_close_summary(today_str)
            _redis.set(CLOSE_MSG_KEY + today_str, "1", ex=86400)

    # Sleep message (16:30)
    if time_now == SLEEP_TIME:
        if not _redis.get(SLEEP_MSG_KEY + today_str):
            send_sleep_message()
            _redis.set(SLEEP_MSG_KEY + today_str, "1", ex=86400)

    # If none of the above, just log that we did nothing.
    logger.info("Scheduler tick completed.")


if __name__ == "__main__":
    main()