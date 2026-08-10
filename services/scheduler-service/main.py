"""
Scheduler Service
-------------------
Single responsibility: trigger analysis for all watchlist stocks in parallel,
detect decision changes worth notifying about, poll the Event Tracker for
material corporate events, and write the end-of-day report.

Market hours (NSE): 09:15-15:30 IST. Scans run every 30 minutes during the
window (1hr before open to 1hr after close).

Notification rule: only notify when a symbol's decision newly becomes BUY NOW,
or flips from a BUY-family decision to SELL. All other scans are silent.

Keep-alive: pings all Render services every 14 minutes so free-tier
instances never spin down during the day.
"""
import os
import json
import logging
import threading
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
import uvicorn

try:
    from upstash_redis import Redis
except ImportError:
    Redis = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler-service")

# ── Environment & constants ────────────────────────────────────────────────────
API_GATEWAY_URL   = os.getenv("API_GATEWAY_URL",   "https://api-gateway-wizr.onrender.com").rstrip("/")
EVENT_TRACKER_URL = os.getenv("EVENT_TRACKER_URL", "https://event-tracker-service-m1lw.onrender.com").rstrip("/")
NOTIFICATION_URL  = os.getenv("NOTIFICATION_URL",  "https://notification-service-36py.onrender.com").rstrip("/")
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "30"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "20"))          # parallel threads
REPORTS_DIR  = os.getenv("REPORTS_DIR", "/tmp/reports")
IST = ZoneInfo("Asia/Kolkata")

SCAN_WINDOW_START = dtime(8, 15)   # 1hr before market open
SCAN_WINDOW_END   = dtime(16, 30)  # 1hr after market close

# Redis for cross‑service state (shared with GitHub Actions runner)
STATE_KEY = "stockky:scheduler:last_decisions"
EOD_KEY_PREFIX = "stockky:scheduler:eod:"
LAST_SCAN_KEY = "stockky:scheduler:last_scan_timestamp"   # new: timestamp of last scan

_redis = None
if Redis is not None:
    try:
        _redis = Redis(
            url=os.getenv("UPSTASH_REDIS_REST_URL"),
            token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
        )
        _redis.ping()
        logger.info("Connected to Upstash Redis — decision state is shared")
    except Exception as e:
        logger.warning("Redis unavailable (%s) — falling back to local file. Duplicate notifications possible.", e)
        _redis = None

STATE_PATH = os.getenv("SCHEDULER_STATE_PATH", "/tmp/reports/last_decisions.json")
os.makedirs(REPORTS_DIR, exist_ok=True)

BUY_FAMILY = {"BUY NOW", "PREPARE TO BUY", "HOLD"}

# Keep‑alive endpoints for all services
MARKET_DATA_URL     = os.getenv("MARKET_DATA_URL", "https://stockky-market-data.onrender.com").rstrip("/")
TECHNICAL_URL       = os.getenv("TECHNICAL_URL", "https://technical-analysis-service-zhnc.onrender.com").rstrip("/")
FUNDAMENTAL_URL     = os.getenv("FUNDAMENTAL_URL", "https://fundamental-analysis-service.onrender.com").rstrip("/")
DECISION_URL        = os.getenv("DECISION_URL", "https://decision-engine-service-0hg6.onrender.com").rstrip("/")
PREDICTION_URL      = os.getenv("PREDICTION_URL", "https://prediction-service-wowb.onrender.com").rstrip("/")
NEWS_URL            = os.getenv("NEWS_URL", "https://news-intelligence-service.onrender.com").rstrip("/")

KEEPALIVE_ENDPOINTS = [
    f"{url}/health" for url in (
        MARKET_DATA_URL, TECHNICAL_URL, API_GATEWAY_URL, DECISION_URL,
        EVENT_TRACKER_URL, PREDICTION_URL, NOTIFICATION_URL,
        FUNDAMENTAL_URL, NEWS_URL,
    )
]

app = FastAPI(title="Stockky Scheduler Service", version="1.1.0")

# ── Helper functions ───────────────────────────────────────────────────────────
def is_market_open(now: datetime) -> bool:
    """Check if current time is within NSE trading hours (Mon-Fri, 09:15-15:30 IST)."""
    if now.weekday() >= 5:
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)

def within_scan_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return SCAN_WINDOW_START <= now.time() <= SCAN_WINDOW_END

def _load_last_decisions() -> dict:
    if _redis:
        try:
            val = _redis.get(STATE_KEY)
            return json.loads(val) if val else {}
        except Exception as e:
            logger.warning("Could not load previous decisions from Redis: %s", e)
            return {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}

def _save_last_decisions(decisions: dict):
    if _redis:
        try:
            _redis.set(STATE_KEY, json.dumps(decisions))
            return
        except Exception as e:
            logger.warning("Could not persist decisions to Redis: %s", e)
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
        logger.warning("Notification dispatch failed: %s", e)

def _check_decision_changes(all_results: list):
    """Compare this scan's decisions against the previous scan's and notify
    only on the two transitions the spec cares about."""
    previous = _load_last_decisions()
    current  = {r["symbol"]: r["decision"] for r in all_results if "decision" in r}

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

def get_watchlist() -> list:
    """Fetch the current watchlist from API Gateway."""
    try:
        resp = httpx.get(f"{API_GATEWAY_URL}/watchlist", timeout=15)
        resp.raise_for_status()
        return resp.json().get("symbols", [])
    except Exception as e:
        logger.error("Failed to fetch watchlist: %s", e)
        return []

def analyze_one(symbol: str) -> dict:
    """Call API Gateway's /analyze endpoint for a single symbol."""
    try:
        resp = httpx.get(f"{API_GATEWAY_URL}/analyze/{symbol}", timeout=60)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Error analyzing %s: %s", symbol, e)
        return {"symbol": symbol, "decision": "ERROR", "error": str(e)}

# ── FastAPI endpoints ──────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "service": "Stockky Scheduler Service",
        "version": "1.1.0",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/scan": "POST – trigger scan manually",
        },
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "scheduler-service"}

@app.post("/scan")
async def manual_scan():
    """Manually trigger a scan (for testing)."""
    threading.Thread(target=run_scan_job, daemon=True).start()
    return {"status": "scan started"}

# ── Scheduler jobs ─────────────────────────────────────────────────────────────
def run_keepalive_job():
    """Ping every service health endpoint every 14 minutes."""
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
    """Parallel scan of all watchlist stocks."""
    now = datetime.now(IST)
    if not within_scan_window(now):
        logger.info("Outside scan window (%s IST) — skipping scan.", now.strftime("%H:%M"))
        return

    symbols = get_watchlist()
    if not symbols:
        logger.warning("No symbols in watchlist – skipping scan.")
        return

    logger.info("Starting parallel scan of %d symbols", len(symbols))
    start = datetime.now()

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {
            executor.submit(analyze_one, sym): sym for sym in symbols
        }
        for future in as_completed(future_to_symbol):
            sym = future_to_symbol[future]
            try:
                data = future.result(timeout=90)
                # data should contain 'symbol' and 'decision'
                results.append(data)
            except Exception as e:
                logger.error("Future error for %s: %s", sym, e)
                results.append({"symbol": sym, "decision": "ERROR", "error": str(e)})

    # Filter out errors for decision change detection
    valid_results = [r for r in results if r.get("decision") not in ("ERROR", None)]
    if valid_results:
        _check_decision_changes(valid_results)

    elapsed = (datetime.now() - start).total_seconds()
    logger.info("Scan completed in %.2f seconds for %d symbols", elapsed, len(symbols))

    # Store summary and last‑scan timestamp (for GitHub Actions coordination)
    try:
        summary = {
            "timestamp": now.isoformat(),
            "scanned": len(symbols),
            "buy_now": [r["symbol"] for r in valid_results if r.get("decision") == "BUY NOW"],
            "sell": [r["symbol"] for r in valid_results if r.get("decision") == "SELL"],
            "hold": [r["symbol"] for r in valid_results if r.get("decision") == "HOLD"],
            "do_not_buy": [r["symbol"] for r in valid_results if r.get("decision") == "DO NOT BUY"],
        }
        if _redis:
            _redis.setex("stockky:last_scan_summary", 3600, json.dumps(summary))
            # Write last scan timestamp (ISO format) – used by GitHub Actions
            _redis.set(LAST_SCAN_KEY, now.isoformat())
            logger.info("Last scan timestamp written to Redis")
    except Exception as e:
        logger.warning("Could not store scan summary or timestamp: %s", e)

def run_event_check_job():
    """Poll Event Tracker for changes and notify."""
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
        logger.warning("Event check failed: %s", e)

def sync_event_subscriptions():
    """Make sure Event Tracker watches the same symbols as the scan watchlist."""
    try:
        wl = httpx.get(f"{API_GATEWAY_URL}/watchlist", timeout=15).json()
        httpx.post(
            f"{EVENT_TRACKER_URL}/subscribe",
            json={"symbols": wl["symbols"]},
            timeout=15,
        )
        logger.info("Event Tracker subscriptions synced: %s", wl["symbols"])
    except httpx.HTTPError as e:
        logger.warning("Could not sync event subscriptions at startup: %s", e)

def run_end_of_day_report():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return

    date_key = EOD_KEY_PREFIX + now.strftime("%Y-%m-%d")
    if _redis:
        try:
            if _redis.get(date_key):
                logger.info("EOD report already sent today – skipping")
                return
        except Exception as e:
            logger.warning("Could not check EOD-sent flag, proceeding: %s", e)

    # Fetch latest scan summary (or run a fresh scan)
    try:
        summary = None
        if _redis:
            raw = _redis.get("stockky:last_scan_summary")
            if raw:
                summary = json.loads(raw)
        if not summary:
            # fallback: run a scan now (but we want to avoid extra load; use summary)
            logger.warning("No scan summary found, falling back to fresh scan for EOD report")
            run_scan_job()  # this updates the summary
            if _redis:
                raw = _redis.get("stockky:last_scan_summary")
                summary = json.loads(raw) if raw else {}
    except Exception as e:
        logger.error("EOD report failed to fetch summary: %s", e)
        return

    report = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(),
        "market_summary": f"Scanned {summary.get('scanned', 0)} stocks",
        "buy_recommendations": summary.get("buy_now", []),
        "stocks_avoided": summary.get("do_not_buy", []),
        "scanned_count": summary.get("scanned", 0),
    }

    path = os.path.join(REPORTS_DIR, f"{report['date']}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    logger.info("End-of-day report saved: %s", path)

    _notify(
        "📊 End-of-day report ready",
        f"{report['market_summary']} — {report['scanned_count']} stocks scanned.",
    )

    if _redis:
        try:
            _redis.set(date_key, "1")
        except Exception as e:
            logger.warning("Could not mark EOD report as sent: %s", e)

# ── Scheduler startup ──────────────────────────────────────────────────────────
def start_scheduler():
    sync_event_subscriptions()
    run_keepalive_job()  # warm up

    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(run_keepalive_job, "interval", minutes=14, id="keepalive")
    scheduler.add_job(run_scan_job, "interval", minutes=SCAN_INTERVAL_MINUTES, id="market_scan")
    scheduler.add_job(run_event_check_job, "interval", hours=2, id="event_check")
    scheduler.add_job(run_end_of_day_report, "cron", hour=16, minute=0, day_of_week="mon-fri", id="eod_report")

    scheduler.start()
    logger.info(
        "Scheduler started. Keep-alive every 14min. Market scan every %s min during IST market window.",
        SCAN_INTERVAL_MINUTES,
    )
    import threading
    event = threading.Event()
    event.wait()  # Keep thread alive

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
    scheduler_thread.start()

    port = int(os.environ.get("PORT", 8009))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)