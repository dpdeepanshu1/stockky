"""
Scheduler — single-shot mode, driven by GitHub Actions cron.

Enhanced with timed notifications:
  - 08:15 IST: "Market opens in 1 hour" + top picks from previous day.
  - 09:15 IST: "Market is open now"
  - Hourly scans (08:20..15:20) with top picks
  - 15:30 IST: Market close summary
  - 16:30 IST: "Going to sleep" + preview for tomorrow
Uses Redis to track sent messages (so each event fires only once per day).

Coordination with Render scheduler service:
  - Before performing a scan, we check Redis key "stockky:scheduler:last_scan_timestamp"
  - If the timestamp is within the last SCAN_INTERVAL_MINUTES (60 min), we skip
    the scan and its notifications to avoid duplication.
  - Otherwise, we run the scan (fallback) by analyzing each symbol individually
    in parallel (more reliable than the monolithic /scan endpoint).

Rate‑limiting considerations:
  - Default concurrency is 5 symbols at a time (MAX_WORKERS).
  - A 1‑second delay between batches (BATCH_DELAY) prevents overwhelming free‑tier APIs.

Manual override:
  - If FORCE_SCAN=true, the window check is bypassed (for manual testing).
  - A start notification is sent once per day at the first run.
  - Overall scan timeout is 1 hour; partial results are sent if the scan is cut short.
"""
import os
import json
import logging
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

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
SCAN_START = dtime(8, 15)   # widened to allow early start (08:20)
SCAN_END = dtime(15, 30)    # last scan (at close)
OPEN_ANNOUNCE_TIME = dtime(8, 15)   # 1 hour before open
CLOSE_SUMMARY_TIME = dtime(15, 30)
SLEEP_TIME = dtime(16, 30)

# Redis keys
STATE_KEY = "stockky:scheduler:last_decisions"
OPEN_MSG_KEY = "stockky:scheduler:open_msg:"     # + date (sent open-in-1h)
OPEN_NOW_KEY = "stockky:scheduler:open_now:"     # + date (sent market-open)
CLOSE_MSG_KEY = "stockky:scheduler:close_msg:"   # + date (sent close summary)
SLEEP_MSG_KEY = "stockky:scheduler:sleep_msg:"   # + date (sent sleep)
DAILY_PICKS_KEY = "stockky:scheduler:picks:"     # + date (store top picks of the day)
LAST_SCAN_KEY = "stockky:scheduler:last_scan_timestamp"   # written by scheduler service
START_MSG_KEY = "stockky:scheduler:start_msg:"   # + date (sent start message)

# NSE holidays 2026
HOLIDAYS_2026 = [
    "2026-01-26", "2026-03-02", "2026-03-31", "2026-04-02",
    "2026-04-10", "2026-04-14", "2026-05-01", "2026-08-15",
    "2026-10-02", "2026-10-22", "2026-11-14", "2026-11-15", "2026-12-25",
]

_redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
)

BUY_FAMILY = {"BUY NOW", "PREPARE TO BUY", "HOLD"}

# Scan interval (60 minutes)
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "60"))
# Per-symbol timeout (seconds)
SYMBOL_TIMEOUT = int(os.getenv("SYMBOL_TIMEOUT", "120"))
# Max parallel symbols (reduced to avoid rate limits)
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "5"))
# Delay between batches (seconds)
BATCH_DELAY = float(os.getenv("BATCH_DELAY", "1.0"))
# Overall scan timeout – 1 hour (3600 seconds)
SCAN_TIMEOUT_TOTAL = int(os.getenv("SCAN_TIMEOUT_TOTAL", "3600"))
# Force scan (bypass window check)
FORCE_SCAN = os.getenv("FORCE_SCAN", "false").lower() == "true"


def is_holiday(today: datetime) -> bool:
    date_str = today.strftime("%Y-%m-%d")
    if today.weekday() >= 5:
        return True
    return date_str in HOLIDAYS_2026


def _wake_up_services():
    """Ping all critical services to ensure they are awake."""
    services = [
        API_GATEWAY_URL,
        NOTIFICATION_URL,
        EVENT_TRACKER_URL,
    ]
    for url in services:
        try:
            httpx.get(f"{url}/health", timeout=10)
            logger.debug(f"Wake-up ping to {url} succeeded")
        except Exception as e:
            logger.warning(f"Wake-up ping to {url} failed: {e}")


def _notify(title: str, message: str, channel: str = "telegram", retries: int = 3):
    """Send notification with retries, longer timeout, and wake‑up first."""
    _wake_up_services()
    time.sleep(5)

    payload = {"title": title, "message": message, "channel": channel}
    for attempt in range(retries + 1):
        try:
            resp = httpx.post(
                f"{NOTIFICATION_URL}/notify",
                json=payload,
                timeout=60
            )
            if resp.status_code == 200:
                logger.info("Notification sent: %s", title)
                return
            else:
                logger.warning(f"Notification attempt {attempt+1} returned {resp.status_code}")
        except httpx.HTTPError as e:
            logger.warning(f"Notification attempt {attempt+1} failed: {e}")
        if attempt < retries:
            time.sleep(10)
    logger.error("Notification failed after all retries: %s", title)


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


def analyze_one(symbol: str, timeout: int = SYMBOL_TIMEOUT) -> Dict[str, Any]:
    """Fetch analysis for a single symbol from the gateway."""
    try:
        resp = httpx.get(f"{API_GATEWAY_URL}/analyze/{symbol}", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}")
        return {"symbol": symbol, "decision": "ERROR", "error": str(e)}


def run_scan_individual() -> Dict[str, Any]:
    """
    Scan all symbols in parallel using individual /analyze calls.
    Uses a small concurrency and batch delay to avoid rate limits.
    Stops after SCAN_TIMEOUT_TOTAL seconds and returns partial results.
    """
    # Get watchlist
    try:
        wl_resp = httpx.get(f"{API_GATEWAY_URL}/watchlist", timeout=15)
        wl_resp.raise_for_status()
        symbols = wl_resp.json().get("symbols", [])
    except Exception as e:
        logger.error(f"Failed to fetch watchlist: {e}")
        return {}

    if not symbols:
        logger.warning("No symbols in watchlist")
        return {}

    logger.info(f"Scanning {len(symbols)} symbols individually (parallel, max {MAX_WORKERS})")
    start = datetime.now()
    results = []
    timed_out = False

    # Process in batches
    for i in range(0, len(symbols), MAX_WORKERS):
        # Check overall timeout
        elapsed = (datetime.now() - start).total_seconds()
        if elapsed > SCAN_TIMEOUT_TOTAL:
            logger.warning(f"Overall scan timeout of {SCAN_TIMEOUT_TOTAL}s reached. Partial results will be returned.")
            timed_out = True
            break

        batch = symbols[i:i + MAX_WORKERS]
        logger.debug(f"Processing batch {i//MAX_WORKERS + 1}: {batch}")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_symbol = {executor.submit(analyze_one, sym): sym for sym in batch}
            for future in as_completed(future_to_symbol):
                # Check timeout for each future individually
                remaining = SCAN_TIMEOUT_TOTAL - (datetime.now() - start).total_seconds()
                if remaining <= 0:
                    timed_out = True
                    break
                sym = future_to_symbol[future]
                try:
                    data = future.result(timeout=min(SYMBOL_TIMEOUT, remaining + 10))
                    results.append(data)
                except Exception as e:
                    logger.error(f"Future error for {sym}: {e}")
                    results.append({"symbol": sym, "decision": "ERROR", "error": str(e)})
        if timed_out:
            break

        # Delay between batches to avoid rate limits
        if i + MAX_WORKERS < len(symbols):
            time.sleep(BATCH_DELAY)

    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"Individual scan completed in {elapsed:.2f}s for {len(results)} symbols (timed_out={timed_out})")

    # Filter out errors for decision change detection
    valid_results = [r for r in results if r.get("decision") not in ("ERROR", None)]
    if valid_results:
        check_decision_changes(valid_results)

    # Build a summary
    scan_result = {
        "verdict": "Partial scan" if timed_out else "Individual scan completed",
        "all_results": results,
        "recommendations": [
            r for r in valid_results
            if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")
        ][:5],
        "scanned": len(symbols),
        "successful": len(valid_results),
        "elapsed": elapsed,
        "timed_out": timed_out,
    }
    return scan_result


def format_stock_picks(picks: List[Dict]) -> str:
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
    key = DAILY_PICKS_KEY + date_str
    existing = _redis.get(key)
    if existing:
        try:
            existing_picks = json.loads(existing)
        except:
            existing_picks = []
    else:
        existing_picks = []

    symbols = {p["symbol"]: p for p in existing_picks}
    for p in picks:
        symbols[p["symbol"]] = p
    new_list = list(symbols.values())
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


def send_start_message():
    """Send a scheduler tick start notification (every run, not de‑duplicated)."""
    title = "🟢 Scheduler Tick Started"
    msg = f"Stockky scheduler tick at {datetime.now(IST).strftime('%H:%M')} IST"
    # Include yesterday's top picks if available
    yesterday = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
    picks = get_daily_picks(yesterday)
    if picks:
        msg += "\n\n" + format_stock_picks(picks)
    _notify(title, msg)


def send_market_open_announcement():
    """Send 'Market opens in 1 hour' along with the top picks from the previous day."""
    yesterday = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
    picks = get_daily_picks(yesterday)

    if picks:
        picks_msg = format_stock_picks(picks)
        msg = f"The market will open at 09:15 IST. Get ready!\n\n{picks_msg}"
    else:
        msg = "The market will open at 09:15 IST. Get ready!"

    _notify("\U0001F55B Market opens in 1 hour", msg)


def send_market_open_now():
    _notify("\U0001F7E2 Market is now open", "Trading has started. Let's find opportunities!")


def send_scan_picks(picks: List[Dict], timed_out: bool = False):
    timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    if timed_out:
        title = "⏱️ Scan Timed Out (Partial Results)"
        if not picks:
            msg = f"Scan timed out after 1 hour – no actionable stocks found yet.\nTime: {timestamp}"
        else:
            msg = f"Scan timed out after 1 hour – partial picks:\n\n{format_stock_picks(picks)}\n\n⏱️ {timestamp}"
        _notify(title, msg)
    else:
        if not picks:
            _notify(
                "\U0001F6AB No buy signals",
                f"No actionable BUY NOW / PREPARE TO BUY stocks at this hour.\nTime: {timestamp}"
            )
        else:
            msg = format_stock_picks(picks) + f"\n\n⏱️ {timestamp}"
            _notify("\U0001F4C8 Market Scan Update", msg)


def send_close_summary(date_str: str):
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
    _notify("\U0001F634 Going to sleep", "Good night! I'll be back tomorrow before market open. Preview for tomorrow: Keep an eye on global cues and any after-market news.")


def should_skip_scan() -> bool:
    try:
        last_scan_str = _redis.get(LAST_SCAN_KEY)
        if not last_scan_str:
            logger.info("No last scan timestamp found in Redis – will run scan (fallback).")
            return False

        last_scan_time = datetime.fromisoformat(last_scan_str)
        now = datetime.now(IST)
        if (now - last_scan_time) < timedelta(minutes=SCAN_INTERVAL_MINUTES):
            logger.info("Scheduler service already ran a scan at %s (within %d min) – skipping GitHub scan.",
                        last_scan_time.strftime("%H:%M"), SCAN_INTERVAL_MINUTES)
            return True
        else:
            logger.info("Last scan was at %s (more than %d min ago) – running GitHub scan.",
                        last_scan_time.strftime("%H:%M"), SCAN_INTERVAL_MINUTES)
            return False
    except Exception as e:
        logger.warning("Error checking last scan timestamp: %s – will run scan as fallback.", e)
        return False


def main():
    now = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")
    time_now = now.time()

    logger.info("Current IST time: %s", now.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Scan window: %s – %s", SCAN_START.strftime("%H:%M"), SCAN_END.strftime("%H:%M"))
    if FORCE_SCAN:
        logger.info("FORCE_SCAN is enabled – window check will be bypassed.")

    if is_holiday(now):
        logger.info("Market holiday – skipping all activity.")
        return

    sync_event_subscriptions()

    # Send start message every time the runner runs (within window or forced)
    if FORCE_SCAN or (SCAN_START <= time_now <= SCAN_END):
        send_start_message()

    # Timed notifications (each once per day)
    if time_now == OPEN_ANNOUNCE_TIME:
        if not _redis.get(OPEN_MSG_KEY + today_str):
            send_market_open_announcement()
            _redis.set(OPEN_MSG_KEY + today_str, "1", ex=86400)

    if time_now == MARKET_OPEN:
        if not _redis.get(OPEN_NOW_KEY + today_str):
            send_market_open_now()
            _redis.set(OPEN_NOW_KEY + today_str, "1", ex=86400)

    if time_now == CLOSE_SUMMARY_TIME:
        if not _redis.get(CLOSE_MSG_KEY + today_str):
            send_close_summary(today_str)
            _redis.set(CLOSE_MSG_KEY + today_str, "1", ex=86400)

    if time_now == SLEEP_TIME:
        if not _redis.get(SLEEP_MSG_KEY + today_str):
            send_sleep_message()
            _redis.set(SLEEP_MSG_KEY + today_str, "1", ex=86400)

    # Determine if we should run a scan
    should_run = False
    if FORCE_SCAN:
        should_run = True
        logger.info("FORCE_SCAN: running scan regardless of window.")
    elif SCAN_START <= time_now <= SCAN_END:
        should_run = True
    else:
        logger.info("Outside scan window and FORCE_SCAN not set – skipping scan.")

    if should_run:
        if should_skip_scan():
            logger.info("Skipping GitHub scan because scheduler service handled it.")
        else:
            # Quick health check
            try:
                health = httpx.get(f"{API_GATEWAY_URL}/health", timeout=5)
                if health.status_code != 200:
                    logger.error("API Gateway not healthy, aborting scan.")
                    _notify("⚠️ Scan Aborted", f"API Gateway is not healthy at {time_now.strftime('%H:%M')} IST.")
                    return
            except Exception:
                logger.error("API Gateway unreachable, aborting scan.")
                _notify("⚠️ Scan Aborted", f"API Gateway unreachable at {time_now.strftime('%H:%M')} IST.")
                return

            logger.info("Running individual symbol scan (fallback) at %s", time_now.strftime("%H:%M"))
            scan_result = run_scan_individual()

            if scan_result:
                picks = scan_result.get("recommendations", [])
                timed_out = scan_result.get("timed_out", False)
                if picks or timed_out:
                    send_scan_picks(picks, timed_out=timed_out)
                    if picks:
                        store_daily_picks(today_str, picks)
                else:
                    send_scan_picks([])

                run_event_check()
            else:
                logger.error("Scan failed – no results available.")
                _notify(
                    "⚠️ Scan Failed",
                    f"The market scan at {time_now.strftime('%H:%M')} IST could not complete. Please check the API Gateway logs."
                )
    else:
        logger.info("Skipping scan due to window/force settings.")

    logger.info("Scheduler tick completed.")


if __name__ == "__main__":
    main()