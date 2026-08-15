# Stockky — AI-Powered Stock Intelligence Platform

An AI equity research analyst for the Indian stock market. Every query returns
exactly one of five decisions: **BUY NOW · PREPARE TO BUY · HOLD · DO NOT BUY · SELL**.
No maybes.

**Phase 1 + Phase 2 are both built and wired together.** Phase 1 covers the
core decision loop (Technical + Fundamental → Decision). Phase 2 adds News
Intelligence, an Event Tracker, an ML Prediction Engine, and free-webhook
Notifications — and the Decision Engine now actually consumes all of them.
Only Phase 3 (cloud deployment, Auth/multi-user, Postgres persistence) is
left as documented next steps, since those require your own free-tier
account signups that I can't create for you.

## Architecture

```
frontend (React)
      │
      ▼
api-gateway ──► decision-engine-service ──┬──► technical-analysis-service ──► market-data-service ──► Yahoo Finance
      │                                    ├──► fundamental-analysis-service ──►  (same)
      │                                    ├──► news-intelligence-service ──► Google News RSS
      │                                    ├──► event-tracker-service ──► Yahoo Finance (earnings/dividends/splits)
      │                                    └──► prediction-service ──► trained XGBoost model
      │
scheduler-service ──► notification-service ──► Discord / Slack / Telegram (free webhooks)
      (runs /scan every 30 min in market hours, polls event-tracker, detects decision flips)
```

Each box is its own container, own `requirements.txt`, own Dockerfile — matching
the microservices-from-Day-1 requirement. They talk over plain REST for now;
swap to RabbitMQ/Kafka later without touching business logic.

| Service | Port | Responsibility |
|---|---|---|
| `market-data-service` | 8001 | Fetch price history & fundamentals from Yahoo Finance (free), cache in Redis |
| `technical-analysis-service` | 8002 | RSI, MACD, EMA, Supertrend, ADX, Bollinger, S/R → technical score 0-100 |
| `fundamental-analysis-service` | 8003 | Revenue/earnings growth, ROE, debt, margins, valuation → fundamental score 0-100 |
| `decision-engine-service` | 8004 | Combines all available scores into 1 of 5 decisions, sets entry/target/stop-loss |
| `news-intelligence-service` | 8005 | Google News RSS + VADER sentiment → news score 0-100 with headlines |
| `event-tracker-service` | 8006 | Tracks earnings dates/dividends/splits per symbol; diffs against last check |
| `prediction-service` | 8007 | XGBoost model estimating probability of a ~5%+ move in 10 trading days |
| `notification-service` | 8008 | Free Discord/Slack/Telegram webhook delivery, only called on real changes |
| `api-gateway` | 8000 | Frontend's single entry point; runs the watchlist scan; enforces "max 3 or DO NOT BUY" |
| `scheduler-service` | — | Background worker: scans every 30 min in market hours, polls events, notifies on flips, writes EOD report |
| `frontend` | 5173 | React + TypeScript + Tailwind dashboard |

## Prerequisites (all free)

- Docker Desktop (or Docker Engine + Compose) — https://www.docker.com/products/docker-desktop/
- No API keys required to run everything. Notifications are optional and off
  by default (see "Configure notifications" below).

## Run it locally

```bash
git clone <your-repo-url> stockky
cd stockky
docker compose up --build
```

Then open:
- Frontend: http://localhost:5173
- API Gateway docs: http://localhost:8000/docs
- Each service also exposes its own `/docs` (Swagger UI) on its port, e.g. http://localhost:8002/docs

First build takes a few minutes (installing pandas-ta, yfinance, etc.). Subsequent
`docker compose up` is fast.

## Try it

1. **Stock Search (Mode 2):** type `TCS` or `RELIANCE` in the search box → Analyze.
2. **Market Scanner (Mode 1):** click "Run market scan" → scans the built-in watchlist
   (TCS, INFY, HDFCBANK, ICICIBANK, RELIANCE, HCLTECH, COFORGE, ANGELONE, ADANIPOWER,
   BEL, HAL, TATAMOTORS, SBIN) and shows the top 3 conviction picks, or an explicit
   "DO NOT BUY ANY STOCK TODAY" if nothing qualifies.
3. Edit the watchlist: `POST http://localhost:8000/watchlist {"symbols": ["TCS","WIPRO"]}`
4. Inspect individual signals directly (useful while tuning weights):
   - `GET http://localhost:8005/analyze/TCS` — news score + headlines
   - `GET http://localhost:8006/events/TCS` — next earnings date, last dividend/split
   - `GET http://localhost:8007/predict/TCS` — model probability (after training, see below)
5. A decision now includes `news_score`, `prediction_score`, and `event_risk`
   — all `null`/`false` gracefully if those services are down or the model
   isn't trained yet, so the platform never breaks, it just knows less.

## Train the Prediction model (do this once)

The Prediction Service ships without a trained model — `/predict/{symbol}`
will honestly say so until you train one. Training builds its own labeled
dataset from 5 years of free Yahoo Finance history (no need to wait weeks
collecting live data):

```bash
docker compose up --build -d market-data-service   # needs to be running
docker compose run --rm prediction-service python train.py
```

This takes a few minutes (pulls 5y history for ~25 NSE stocks, computes
indicators, trains an XGBoost classifier) and prints a classification report
+ ROC-AUC so you can judge quality before trusting it. It saves `model.pkl`
inside the service folder. Restart the service to pick it up:

```bash
docker compose restart prediction-service
```

Re-run `train.py` periodically (e.g. monthly) as more market data accumulates.
Widen `TRAINING_UNIVERSE` in `services/prediction-service/train.py` to
improve generalization — more symbols and sectors is almost always better
than more history on the same 13 stocks.

## Configure notifications (optional)

Pick any (or none) of these free channels, then restart
`notification-service` + `scheduler-service`:

- **Discord**: Server Settings → Integrations → Webhooks → New Webhook → copy URL → `DISCORD_WEBHOOK_URL`
- **Slack**: api.slack.com/apps → your app → Incoming Webhooks → copy URL → `SLACK_WEBHOOK_URL`
- **Telegram**: message @BotFather → `/newbot` → get token → `TELEGRAM_BOT_TOKEN`; message your bot once, then fetch your chat ID from `https://api.telegram.org/bot<token>/getUpdates` → `TELEGRAM_CHAT_ID`

Put these in a `.env` file at the repo root (copy `.env.example`), then:

```bash
docker compose up -d --build
```

You'll get a message only when: a symbol newly becomes BUY NOW, an existing
BUY-family position flips to SELL, a tracked earnings/dividend/split changes,
or the end-of-day report completes.

## What Phase 2 does NOT cover yet (honest gaps)

- **Bulk deals, block deals, insider trading, board meetings, exchange
  filings** — these live on NSE/BSE's own announcement pages, which change
  markup often enough to deserve a dedicated, actively-maintained scraper.
  The Event Tracker currently covers earnings dates, dividends, and splits
  via Yahoo Finance instead — solid but narrower than the full spec.
- **Prediction accuracy** — the model trains on 25 liquid large/mid-caps
  over 5 years. Treat its `prediction_score` as one more input, not a
  guarantee; check the ROC-AUC printed during training before trusting it.
- **Persistence** — watchlist and event-tracker state are stored in-memory /
  local JSON files, not a database yet. Fine for one user on one machine;
  see Phase 3 below for Postgres.

## Deploying free (Phase 3 — not yet built, here's the path)

The remaining work is Auth/multi-user + real cloud hosting. Both need your
own free account signups, so I've laid out the exact steps rather than code:

- **Frontend →Vercel**: `vercel.com`, free tier, connect your GitHub repo, root
  directory `frontend/`, framework preset "Vite".
- **Python services → Render**: `render.com` free web services, one per
  service, build command `pip install -r requirements.txt`, start command
  from each Dockerfile's CMD. Free tier spins down when idle — fine for MVP.
- **Postgres → Neon or Supabase**: both have free tiers; use for watchlists,
  users, event subscriptions, scan history once you add those tables.
- **Redis → Upstash**: free tier, just swap `REDIS_URL` env var.
- **CI/CD → GitHub Actions**: free for public repos; add a workflow that runs
  `docker compose build` on push to catch breakages before deploying.
- **Auth + User Service**: add `services/auth-service` in Spring Boot +
  Spring Security + JWT, backed by the same free Postgres instance, once you
  have more than one user. The API Gateway is the natural place to enforce
  the JWT check before proxying to Decision Engine.
- **Move watchlist/event-tracker state to Postgres**: replace the in-memory
  list in `api-gateway/main.py` and the JSON file in
  `event-tracker-service/main.py` with simple SQLAlchemy models once you're
  on a real database — the function signatures don't need to change.

## Notes on data & compliance

Yahoo Finance data via `yfinance` is used for convenience in the MVP under its
public terms; for anything beyond personal/research use, prefer official NSE/
BSE APIs or a licensed data vendor. This platform is informational — it is not
investment advice.


# Stockky — Changes Manifest

This folder contains every file edited or created across this session, at its
correct repo path. Files not listed here (the other 7 backend services, the
rest of the frontend, prediction-service) were read for context but not
modified — your base copies are already correct.

## services/training-service/
- **app.py** — CORS added; `from evaluator import` typo fixed (`evaluate`);
  automatic T+1/T+5 evaluation scheduling on every recorded prediction;
  `get_training_status()` now reads durable DB state (TrainingRun +
  ModelRegistry) instead of ephemeral local files; dedup guard on
  `store_prediction` (same symbol+decision+day = same pick, regardless of
  caller); six previously-dropped fields wired through
  (`market_sentiment_adjustment`, `holding_period`, `support`, `resistance`,
  `sector`, `valuation`, `feature_snapshot`); new endpoints:
  `/api/metrics/daily`, `/api/metrics/weekly`, `/api/actionable/commit`,
  `/api/trades*`, `/api/portfolio/*`, `/api/stock/history/{symbol}`,
  `/api/train/progress`, `/api/lock/clear`.
- **train.py** — new `train_pick_success_model()`: trains a classifier on
  the system's own real BUY/PREPARE TO BUY picks and their real outcomes,
  replacing the old OHLCV regressor (kept, unused, as `--legacy-ohlcv`).
  Champion/challenger promotion (compares new model's F1 against current
  production before replacing it). `label_source` toggle (`t1_outcome` /
  `trade_pnl`). Live stage-by-stage progress tracking for the animated UI.
  Fixed a real crash-risk: all-NaN feature columns (rsi/volume_ratio are
  currently always null) were reaching the scaler unguarded.
- **evaluate.py** — `update_prediction_success()` was defined but never
  called anywhere — now wired into `evaluate_t1`/`evaluate_t5`. Fixed 0/1/2
  labeling: real failures were being written identically to "not yet
  evaluated", silently biasing both the KNN search and the classifier.
- **scanner.py** — now loads and scores with the trained model (gated by
  `model_type` so a stale non-classifier artifact can't get used by
  accident). Fixed the same NaN-propagation bug as train.py, in the KNN
  distance calculation specifically — was producing meaningless neighbor
  rankings on every call given today's data gaps.
- **models.py** — added `PaperTrade`, `PortfolioAccount`,
  `PortfolioTransaction` tables, with `ensure_schema` migration entries so
  they actually get added via `ALTER TABLE` on an already-deployed DB.
- **trades.py** — new file. Paper trading against one shared dummy balance
  (not a fresh pot per trade). Weekly-cycle exits: target/stop-loss exit
  immediately, otherwise reviewed every 7 days, closed if already up 3%+,
  held into next week otherwise, 21-day hard cap.

## services/api-gateway/main.py
- Value-adjusted top-pick ranking (₹2000 cap + fundamentals-weighted bonus
  for low-price stocks) at all 3 scan finalization points.
- Real scan cancellation (`POST /scan/cancel/{task_id}`) — checked
  periodically inside `run_scan_parallel`, finalizes with whatever was
  actually scored so far instead of an empty result.
- Self-pruning scan universe — symbols with 10 consecutive non-actionable
  scans get excluded from future universe builds (watchlist exempt), so the
  universe actually evolves instead of a static list reshuffling.
- Event data passthrough fixed — was discarding everything except
  `next_earnings_date`; now passes the full raw dict through.
- Precise holding-period date-range estimates, alongside the existing
  (often static) `holding_period` string.
- Working async Gemini summary generator with truncation detection
  (`finishReason == MAX_TOKENS`) and clean fallback to the existing
  template — only wired into the async scan path, which has a client
  available; the two sync paths still use the template only.

## services/scheduler-service/run_once.py
- Fixed a real bug: daily/Telegram picks were taken in arbitrary batch-
  completion order, not ranked by score at all. Now uses the same
  value-adjusted ranking as the gateway.

## services/fundamental-analysis-service/indianapi_fallback.py
- New file, not yet integrated (that service's `main.py` was read but the
  call site was never spliced in — needs your confirmation of exactly
  where the existing Yahoo Finance fetch lives). IndianAPI fallback used
  only when Yahoo fails, 5-trading-day cache aligned to NSE market open,
  rate-limited to 1 req/sec via Redis. Uses `upstash_redis.Redis`
  (confirmed via the actual codebase, not guessed).

## frontend/src/
- **api.ts** — fixed a systemic path bug: every method I'd added was
  missing the `/api/` prefix the gateway's catch-all proxy requires.
  Cross-checked against the gateway's actual routing this time.
- **App.tsx** — real Stop Scan button wired to the now-real cancel
  endpoint; Trades tab registered in navigation.
- **components/ScanPanel.tsx** — "Add All Actionable to Training" button;
  value-adjusted picks section; "all actionable" list also sorted by
  value-adjusted score, not raw order.
- **components/Training.tsx** — animated stage-tracker panel polling
  `/api/train/progress`; daily/weekly pick-tracking card; manual T+1/T+5
  evaluation trigger buttons (fallback for when scheduler isn't running).
- **components/Trades.tsx** — full portfolio-page rewrite: balance header,
  add-funds modal, expandable position cards with inline charts, daily/
  weekly trade reports.
- **components/StockChart.tsx** — new file. 1D/5D/1M/1Y/5Y price chart
  using `recharts` (already a project dependency).
- **components/DecisionCard.tsx** — "Trade This" button + confirmation
  modal; model recommendation panel (training-service's real signal,
  separate from `combined_score`); event data rendering; holding-period
  estimate display.

## Known open items (need more files or your decision)
- `technical-analysis-service` still doesn't populate `rsi`/`macd`/`ema`/
  `volume_ratio` in the payload to training-service — I have that file now
  but haven't yet made this specific fix.
- `market-sentiment-service` is defined as a URL in api-gateway but never
  actually called anywhere — a real, previously-hidden integration gap.
- `indianapi_fallback.py` needs wiring into `fundamental-analysis-service`'s
  actual Yahoo-fetch call site.
- Whether `TRAINING_DATABASE_URL` is actually a persistent Postgres in your
  Render deployment, or still the SQLite default — this determines whether
  any of the training-service work here actually survives a restart.
# Stockky — Changes Manifest

This folder contains every file edited or created across this session, at its
correct repo path. Files not listed here (the other 7 backend services, the
rest of the frontend, prediction-service) were read for context but not
modified — your base copies are already correct.

## services/training-service/
- **app.py** — CORS added; `from evaluator import` typo fixed (`evaluate`);
  automatic T+1/T+5 evaluation scheduling on every recorded prediction;
  `get_training_status()` now reads durable DB state (TrainingRun +
  ModelRegistry) instead of ephemeral local files; dedup guard on
  `store_prediction` (same symbol+decision+day = same pick, regardless of
  caller); six previously-dropped fields wired through
  (`market_sentiment_adjustment`, `holding_period`, `support`, `resistance`,
  `sector`, `valuation`, `feature_snapshot`); new endpoints:
  `/api/metrics/daily`, `/api/metrics/weekly`, `/api/actionable/commit`,
  `/api/trades*`, `/api/portfolio/*`, `/api/stock/history/{symbol}`,
  `/api/train/progress`, `/api/lock/clear`.
- **train.py** — new `train_pick_success_model()`: trains a classifier on
  the system's own real BUY/PREPARE TO BUY picks and their real outcomes,
  replacing the old OHLCV regressor (kept, unused, as `--legacy-ohlcv`).
  Champion/challenger promotion (compares new model's F1 against current
  production before replacing it). `label_source` toggle (`t1_outcome` /
  `trade_pnl`). Live stage-by-stage progress tracking for the animated UI.
  Fixed a real crash-risk: all-NaN feature columns (rsi/volume_ratio are
  currently always null) were reaching the scaler unguarded.
- **evaluate.py** — `update_prediction_success()` was defined but never
  called anywhere — now wired into `evaluate_t1`/`evaluate_t5`. Fixed 0/1/2
  labeling: real failures were being written identically to "not yet
  evaluated", silently biasing both the KNN search and the classifier.
- **scanner.py** — now loads and scores with the trained model (gated by
  `model_type` so a stale non-classifier artifact can't get used by
  accident). Fixed the same NaN-propagation bug as train.py, in the KNN
  distance calculation specifically — was producing meaningless neighbor
  rankings on every call given today's data gaps.
- **models.py** — added `PaperTrade`, `PortfolioAccount`,
  `PortfolioTransaction` tables, with `ensure_schema` migration entries so
  they actually get added via `ALTER TABLE` on an already-deployed DB.
- **trades.py** — new file. Paper trading against one shared dummy balance
  (not a fresh pot per trade). Weekly-cycle exits: target/stop-loss exit
  immediately, otherwise reviewed every 7 days, closed if already up 3%+,
  held into next week otherwise, 21-day hard cap.

## services/api-gateway/main.py
- Value-adjusted top-pick ranking (₹2000 cap + fundamentals-weighted bonus
  for low-price stocks) at all 3 scan finalization points.
- Real scan cancellation (`POST /scan/cancel/{task_id}`) — checked
  periodically inside `run_scan_parallel`, finalizes with whatever was
  actually scored so far instead of an empty result.
- Self-pruning scan universe — symbols with 10 consecutive non-actionable
  scans get excluded from future universe builds (watchlist exempt), so the
  universe actually evolves instead of a static list reshuffling.
- Event data passthrough fixed — was discarding everything except
  `next_earnings_date`; now passes the full raw dict through.
- Precise holding-period date-range estimates, alongside the existing
  (often static) `holding_period` string.
- Working async Gemini summary generator with truncation detection
  (`finishReason == MAX_TOKENS`) and clean fallback to the existing
  template — only wired into the async scan path, which has a client
  available; the two sync paths still use the template only.

## services/scheduler-service/run_once.py
- Fixed a real bug: daily/Telegram picks were taken in arbitrary batch-
  completion order, not ranked by score at all. Now uses the same
  value-adjusted ranking as the gateway.

## services/fundamental-analysis-service/indianapi_fallback.py
- New file, not yet integrated (that service's `main.py` was read but the
  call site was never spliced in — needs your confirmation of exactly
  where the existing Yahoo Finance fetch lives). IndianAPI fallback used
  only when Yahoo fails, 5-trading-day cache aligned to NSE market open,
  rate-limited to 1 req/sec via Redis. Uses `upstash_redis.Redis`
  (confirmed via the actual codebase, not guessed).

## frontend/src/
- **api.ts** — fixed a systemic path bug: every method I'd added was
  missing the `/api/` prefix the gateway's catch-all proxy requires.
  Cross-checked against the gateway's actual routing this time.
- **App.tsx** — real Stop Scan button wired to the now-real cancel
  endpoint; Trades tab registered in navigation.
- **components/ScanPanel.tsx** — "Add All Actionable to Training" button;
  value-adjusted picks section; "all actionable" list also sorted by
  value-adjusted score, not raw order.
- **components/Training.tsx** — animated stage-tracker panel polling
  `/api/train/progress`; daily/weekly pick-tracking card; manual T+1/T+5
  evaluation trigger buttons (fallback for when scheduler isn't running).
- **components/Trades.tsx** — full portfolio-page rewrite: balance header,
  add-funds modal, expandable position cards with inline charts, daily/
  weekly trade reports.
- **components/StockChart.tsx** — new file. 1D/5D/1M/1Y/5Y price chart
  using `recharts` (already a project dependency).
- **components/DecisionCard.tsx** — "Trade This" button + confirmation
  modal; model recommendation panel (training-service's real signal,
  separate from `combined_score`); event data rendering; holding-period
  estimate display.

## Known open items (need more files or your decision)
- `technical-analysis-service` still doesn't populate `rsi`/`macd`/`ema`/
  `volume_ratio` in the payload to training-service — I have that file now
  but haven't yet made this specific fix.
- `market-sentiment-service` is defined as a URL in api-gateway but never
  actually called anywhere — a real, previously-hidden integration gap.
- `indianapi_fallback.py` needs wiring into `fundamental-analysis-service`'s
  actual Yahoo-fetch call site.
- Whether `TRAINING_DATABASE_URL` is actually a persistent Postgres in your
  Render deployment, or still the SQLite default — this determines whether
  any of the training-service work here actually survives a restart.

---

# Round 2 — production-reported bugs + decision logic

## services/api-gateway/main.py
- **Fixed the real Stop Scan bug** (confirmed via isolated Python test,
  not guessed): `tasks = [_analyze_one_symbol_ultra(...) for sym in
  universe]` created bare coroutine objects, but the cancellation code
  called `.done()`/`.cancel()` on them — coroutines don't have those
  methods. This raised an unhandled `AttributeError` the instant Stop
  Scan was clicked, silently killing the entire background scan task
  before it ever wrote the finalized "done" status. That's exactly why
  it showed "Stopping — finishing up..." forever with no summary. Fixed
  by wrapping each coroutine in a real `asyncio.Task` via
  `asyncio.ensure_future()`.
- **Fixed a second bug in the same area**: the cancel flag lived in the
  same Redis dict that periodic progress writes were overwriting wholesale
  every 5 completions — a cancel request could get silently wiped before
  the next check noticed it. Now a separate, dedicated Redis key.
- **Enabled caching for technical-analysis-service** (see below) via
  `docker-compose.yml` — every symbol analysis in every scan was
  previously hitting market-data-service and recomputing every indicator
  fresh, with zero caching benefit, despite a full TTL-aware cache system
  already being built into that service and just never receiving
  credentials.

## services/technical-analysis-service/main.py
- **Fixed a live bug**: `import json` only happened inside
  `if __name__ == "__main__":`, which never executes under
  `uvicorn main:app` (the actual `Dockerfile` command). Every call to
  the cache helpers would have raised `NameError: name 'json' is not
  defined` the moment Redis credentials were configured for this
  service — which they weren't (see docker-compose.yml fix below),
  so the bug was dormant, not absent.
- Added `volume_ratio` to the output — already computed internally
  (`vol_now`/`vol_avg20`), just never included in the response. This is
  the field decision-engine needed and couldn't get.

## services/decision-engine-service/main.py
- **Softened the rigid all-must-pass BUY NOW gate** (your explicit
  highest-priority item): previously required
  `technical>=60 AND fundamental>=50 AND trend_strength AND volume_surge
  AND resistance_ok AND news_ok AND model_ok` all non-negotiably — one
  merely-average input (like normal-not-surging volume) could veto an
  otherwise excellent setup. Replaced with combined-score thresholds
  (`combined>=72` for BUY NOW, `combined>=60` or strong-fundamentals for
  PREPARE TO BUY) while keeping resistance/news/model as genuine hard
  safety gates.
- **Fixed the long-flagged rsi/volume_ratio gap** (raised at the very
  start of this whole session): `record_prediction_for_training`'s
  payload hardcoded these to `None` even though the raw `technical` dict
  was in scope the whole time — it just was never referenced. Now wired
  through. Also derives an `ema` alignment label from the three EMA
  values technical-analysis-service actually provides (no fabricated
  `macd` value — that service doesn't compute one at all, left `None`
  honestly rather than inventing something).
- Replaced the hardcoded `"holding_period": "2-6 weeks"` (always, for
  every stock) with a computed range based on actual target distance,
  in both the main response and the training-service payload.
- **New**: `long_term_hold` flag + `long_term_hold_estimate` (6-18 month
  date range) — a signal separate from the short-term BUY NOW/PREPARE TO
  BUY decision, since a stock can be a strong long-term candidate
  independent of current entry timing.

## docker-compose.yml
- **Fixed the hardcoded Upstash Redis credentials** flagged at the very
  start of this session — were sitting in plaintext across 5 services.
  Now `${UPSTASH_REDIS_REST_URL}`/`${UPSTASH_REDIS_REST_TOKEN}`, matching
  the `${VAR}` pattern already used elsewhere in this same file for other
  secrets. Requires a `.env` file (not committed to git) with these two
  values for `docker compose up` to substitute them.
- Added Redis credentials to `technical-analysis-service` so its
  already-built caching actually activates.

## frontend/src/
- **App.tsx**: scan state now persists across a page refresh
  (`sessionStorage`) — previously reloading mid-scan lost all state and
  dropped back to idle even though the backend scan was still running or
  already sitting done. Stop Scan button now correctly reflects the
  (now-real) cancellation flow.
- **Trades.tsx**: `Promise.all` → `Promise.allSettled` for the initial
  fetch — one failed endpoint was blanking out the entire page instead of
  showing what did load. Timestamps now show date + time (data always
  had it, display was dropping it). Default trade capital 10000 (was
  100000).
- **Every hardcoded "may not be routed through the gateway yet" error
  message replaced with the real propagated error** (5 places: deposit,
  trade open, trade close, mark-to-market, scan cancel) — these were
  guesses I'd written as fallback text months ago in this session; the
  actual `request()` helper already surfaces real HTTP status + body, the
  UI layer was just discarding it.
- **Training.tsx**: fixed the actual "pipeline not correct" bug — when
  `/api/train/progress` returns nothing (stale deployment or a real
  failure), every stage dot rendered as plain "pending" with nothing
  highlighted, looking like a static list rather than a live pipeline.
  Now shows "Connecting..." and lights the first stage instead of a
  flat unlit row. Added a rough ETA (linear extrapolation from stages
  completed so far, same approach the scan ETA already uses).
- **ScanPanel.tsx / api.ts / App.tsx**: two new bulk actions — "Add Top
  Picks to Watchlist" and "Add All Actionable to Watchlist" — reusing
  api-gateway's existing `/watchlist/add` endpoint, which already dedupes
  server-side via a Python set.
- **DecisionCard.tsx**: "Highly Recommend for Long Term Hold" badge with
  its estimated hold date range, displayed separately from the short-term
  decision.

## services/training-service/trades.py
- Default trade capital: 100000 → 10000, with manual override still
  available (capital param / Add Funds).
- **New**: dynamic capital sizing — scales the default trade size up as
  the account actually proves a real edge (realized P&L growing), never
  scales down reactively on a losing stretch, hard-capped at 15% of
  current cash balance regardless of how the performance scaling
  computes, so a winning streak can't compound into one oversized bet.
- Weekly take-profit threshold: 3% → 5%.

## Genuinely deferred this round — not attempted, not guessed at
The following were explicitly requested but are each substantial enough
(multi-file, needs real data-source verification, or carries real risk if
rushed) that I'm not attempting a shallow pass:
- Technical scoring overhaul (Supertrend, VWAP, relative strength vs
  Nifty, volume/delivery profile, multi-timeframe confirmation, pivot-
  point support/resistance)
- Fundamental scoring made sector-relative (P/E vs sector median,
  promoter holding, pledging, ROCE consistency, FCF yield)
- Prediction model overhaul (expanded universe, risk-adjusted target,
  new features, monthly retrain cadence, live hit-rate feedback)
- News sentiment (FinBERT/finance-specific model instead of the current
  approach) and keyword-filtered headline relevance
- Event tracker: NSE bulk/block deal feeds, board meetings, credit rating
  changes; Analysis-page "Previous vs Now/Upcoming" section split
- Market context granularity (India VIX, FII/DII flow, breadth)
- Automated outcome validation loop (win rate, R-multiple, drawdown,
  auto-disable if edge disappears)
- Momentum/earnings-surprise scanner running every 5-10 min during market
  hours
- GitHub Actions workflows for scheduled retraining and T+1/T+5 sweeps
  (with the wake-service-first pattern), and for the momentum scanner
- CallMeBot voice-call urgent alerts + `/alert/urgent` endpoint + "Call
  Me Now" button
- New data source integration (GNews/Currents, jugaad-data/nse library,
  Financial Modeling Prep, IndianAPI.in for fundamentals)
- "Clear all from current backup" + "view backup" buttons
- Fundamental analysis panel made more concise
- Service cold-start resilience (parallel wake-up + backfill missing
  parameters once a sleeping service wakes)

If you want to sequence these, your own priority order from the request
(soften decision engine → sector-relative fundamentals → expand
prediction universe/retrain → technical indicators → feedback loop) is
a reasonable one to follow, and item 1 is now done.

# Stockky scan speed fixes — apply instructions

These files keep the **same relative paths** as the Stockky repo root.

## Apply into your existing clone

```bash
cd /path/to/your/stockky   # your local git clone

# Backup first (optional)
cp services/api-gateway/main.py services/api-gateway/main.py.bak
cp services/decision-engine-service/main.py services/decision-engine-service/main.py.bak
cp services/market-data-service/main.py services/market-data-service/main.py.bak

# Copy patched files on top (from this zip extract folder)
cp -r /path/to/stockky_speed_fix/services ./
cp /path/to/stockky_speed_fix/docker-compose.yml ./
cp /path/to/stockky_speed_fix/.env.example ./
cp /path/to/stockky_speed_fix/.github/workflows/scheduler.yml ./.github/workflows/
```

Or from the zip extract directory itself:

```bash
cd stockky_speed_fix
cp -r services docker-compose.yml .env.example /path/to/your/stockky/
cp .github/workflows/scheduler.yml /path/to/your/stockky/.github/workflows/
```

## Env (add to your .env)

```
MAX_PARALLEL_SCAN_WORKERS=18
WAKE_BEFORE_SCAN=true
WAKE_WAIT_SECONDS=8
LAST_FULL_SCAN_TTL=900
DECIDE_CACHE_TTL_OPEN=300
DECIDE_CACHE_TTL_CLOSED=21600
SCAN_LITE_DEFAULT=false
YFINANCE_MAX_CONCURRENT=6
YFINANCE_MIN_INTERVAL_SEC=0.08
DECIDE_BATCH_MAX=25
DECIDE_BATCH_CONCURRENCY=8
```

## Restart

```bash
docker compose up -d --build api-gateway decision-engine-service market-data-service
```

## Scan API

- Normal: POST /scan/start
- Force new: POST /scan/start?force_refresh=true
- Fast free-tier: POST /scan/start?lite=true
