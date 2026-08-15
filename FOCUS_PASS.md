# Focused pass — Bhavcopy + Peer map + Paper-trading UI

## Changed files (paths relative to repo root)

- `services/market-data-service/bhavcopy.py` — **new** full NSE quote + bhavcopy delivery resolver
- `services/market-data-service/main.py` — `/delivery/{symbol}` + `/delivery/{symbol}/refresh`
- `services/fundamental-analysis-service/peers.py` — **new** sector peer map + peer-relative score
- `services/fundamental-analysis-service/main.py` — multi-quarter + peer-relative wired into `/analyze`
- `services/training-service/trades.py` — clear-all + backup helpers
- `services/training-service/app.py` — `/api/trades/clear-backup`, `/api/trades/backups`
- `frontend/src/components/Trades.tsx` — Clear All + Backup, backup list, richer performance cards
- `frontend/src/api.ts` — `clearTradesBackup`, `listTradeBackups`

## Apply

```bash
cd /path/to/your/stockky
cp -r /path/to/stockky_focus_pass/services ./
cp -r /path/to/stockky_focus_pass/frontend ./
```

Restart market-data, fundamental, training services + rebuild frontend.
