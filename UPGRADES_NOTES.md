# Stockky Full Upgrade Notes (Free-tier)

This package upgrades https://github.com/dpdeepanshu1/stockky for:

## Accuracy & decisions
- Multi-horizon scoring: **Short (3–21d) / Mid (1–6m) / Long (6–24m)** with different weights
- Short-term is primary focus (project requirement)
- Soft score-driven decision rules + market regime weight tilt
- Relative strength / delivery / extended / thin-history / liquidity flags
- Closed-loop live win-rate feedback hook into scores
- Scan returns `recommendations_short|mid|long` + `final_verdict`

## Reliability (free tier)
- Decide cache (memory + Upstash Redis)
- yfinance concurrency guard
- Delivery % endpoint (NSE quote best-effort)
- Graceful degradation when optional services fail
- Watchlist dedup: “Already in watchlist”

## Training / Paper trade
- T+1 / T+5 evaluation routes hardened
- Train trigger background job route
- Paper trades **Clear All + Backup** + list backups

## Notifications
- CallMeBot free Telegram call/text (`POST /call/me`)
- Multi-user via `CALLMEBOT_USERS`

## Frontend
- Horizon strip on analysis
- Multi-horizon scan lists + Final Verdict
- Back button on analysis
- Last analysis restore from localStorage
- Scan resume via sessionStorage (existing)

## Deploy
```bash
cp .env.example .env   # fill Upstash + optional CallMeBot
docker compose up --build
```
Frontend: Vercel. Backend services: Render free. Redis: Upstash free. Scheduler: GitHub Actions.

## Remaining intentional gaps (honest)
- Full NSE bhavcopy historical archive parsing still best-effort (API shape changes)
- Peer averages need a sector map; defaults to neutral 50 when peers missing
- Prediction walk-forward/calibration should be re-run via `train.py` after deploy
- Some original services not fully rewritten line-by-line; core decision path is upgraded
