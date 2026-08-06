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
