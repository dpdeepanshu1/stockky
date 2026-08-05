"""
API Gateway
------------
Single entry point the React frontend talks to. Responsibilities:
  - Mode 2 (Stock Search): proxy a single-symbol decision request.
  - Mode 1 (AI Market Scanner): run the watchlist through the Decision Engine
    and return only the highest-conviction results (max 3), or an explicit
    "DO NOT BUY ANY STOCK TODAY" when nothing qualifies.
  - Watchlist CRUD (in-memory for MVP — swap for Postgres in Phase 2).

This service intentionally contains zero analysis logic — it only routes
and applies the "top 3, or none" rule from the product spec.
"""
import os
import logging
from typing import List

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api-gateway")

DECISION_URL = os.getenv("DECISION_URL", "http://decision-engine-service:8004")

app = FastAPI(title="Stockky API Gateway", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Default scan universe for the MVP — a curated basket of liquid NSE large/mid
# caps rather than the full exchange, so a scan finishes in seconds on free
# infra. Expand this list (or load it from Postgres) as you scale up.
DEFAULT_WATCHLIST = [
    "TCS", "INFY", "HDFCBANK", "ICICIBANK", "RELIANCE", "HCLTECH",
    "COFORGE", "ANGELONE", "ADANIPOWER", "BEL", "HAL", "TATAMOTORS", "SBIN",
]

# In-memory store for the MVP — persisted watchlist lives in Postgres in Phase 2.
_watchlist_store: List[str] = list(DEFAULT_WATCHLIST)


class WatchlistUpdate(BaseModel):
    symbols: List[str]


@app.get("/health")
def health():
    return {"status": "ok", "service": "api-gateway"}


@app.get("/watchlist")
def get_watchlist():
    return {"symbols": _watchlist_store}


@app.post("/watchlist")
def set_watchlist(update: WatchlistUpdate):
    _watchlist_store.clear()
    _watchlist_store.extend([s.strip().upper() for s in update.symbols])
    return {"symbols": _watchlist_store}


@app.get("/stock/{symbol}")
def get_stock_decision(symbol: str, already_owned: bool = False):
    """Mode 2: Stock Search — full analysis for one symbol."""
    try:
        resp = httpx.get(
            f"{DECISION_URL}/decide/{symbol}",
            params={"already_owned": already_owned},
            timeout=25,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Decision engine unreachable: {e}")


@app.get("/scan")
def run_scan():
    """Mode 1: AI Market Scanner — analyze the whole watchlist, return only
    the top BUY NOW / PREPARE TO BUY candidates (max 3), ranked by combined
    score. If nothing qualifies, say so explicitly — that is a valid result,
    not an error."""
    results = []
    errors = []

    with httpx.Client(timeout=25) as client:
        for symbol in _watchlist_store:
            try:
                resp = client.get(f"{DECISION_URL}/decide/{symbol}")
                resp.raise_for_status()
                results.append(resp.json())
            except httpx.HTTPError as e:
                logger.warning("Scan skipped %s: %s", symbol, e)
                errors.append({"symbol": symbol, "error": str(e)})

    actionable = [
        r for r in results if r["decision"] in ("BUY NOW", "PREPARE TO BUY")
    ]
    actionable.sort(key=lambda r: r["combined_score"], reverse=True)
    top_picks = actionable[:3]

    return {
        "scanned": len(results),
        "watchlist_size": len(_watchlist_store),
        "recommendations": top_picks,
        "verdict": (
            "DO NOT BUY ANY STOCK TODAY" if not top_picks else f"{len(top_picks)} opportunity(ies) found"
        ),
        "all_results": results,
        "errors": errors,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
