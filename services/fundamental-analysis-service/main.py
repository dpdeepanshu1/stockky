"""
Fundamental Analysis Service
------------------------------
Single responsibility: turn raw fundamental data (fetched from Market Data
Service) into a fundamental quality score (0-100) and readable reasons.
Thresholds below are reasonable general-purpose defaults for Indian large/mid
caps — tune per-sector later (e.g. banks need a different debt-to-equity read
than manufacturers).
"""
import os
import logging

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fundamental-analysis-service")

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://stockky-market-data.onrender.com")

app = FastAPI(title="Stockky Fundamental Analysis Service", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def root():
    return {
        "service": "Stockky Fundamental Analysis Service",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/analyze/{symbol}": "GET – fundamental score for a symbol",
            "/docs": "Swagger UI documentation",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "fundamental-analysis-service"}


def _pct(x):
    """Yahoo returns ratios as decimals (0.15 == 15%); normalize safely."""
    if x is None:
        return None
    return x * 100 if abs(x) < 5 else x


@app.get("/analyze/{symbol}")
def analyze(symbol: str):
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/fundamentals/{symbol}", timeout=15)
        resp.raise_for_status()
        f = resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Market data service unreachable: {e}")

    score = 50
    reasons = []

    rev_growth = _pct(f.get("revenue_growth"))
    if rev_growth is not None:
        if rev_growth > 15:
            score += 12
            reasons.append(f"Revenue growing {rev_growth:.1f}% YoY — strong expansion")
        elif rev_growth > 5:
            score += 5
            reasons.append(f"Revenue growing {rev_growth:.1f}% YoY — steady growth")
        elif rev_growth < 0:
            score -= 12
            reasons.append(f"Revenue declining {rev_growth:.1f}% YoY — red flag")
        else:
            reasons.append(f"Revenue growth flat at {rev_growth:.1f}%")

    earnings_growth = _pct(f.get("earnings_growth"))
    if earnings_growth is not None:
        if earnings_growth > 15:
            score += 12
            reasons.append(f"Earnings growing {earnings_growth:.1f}% YoY — profitable expansion")
        elif earnings_growth < 0:
            score -= 12
            reasons.append(f"Earnings declining {earnings_growth:.1f}% YoY — margin or demand pressure")

    roe = _pct(f.get("roe"))
    if roe is not None:
        if roe > 20:
            score += 10
            reasons.append(f"ROE at {roe:.1f}% — excellent capital efficiency")
        elif roe > 12:
            score += 5
            reasons.append(f"ROE at {roe:.1f}% — healthy capital efficiency")
        elif roe < 8:
            score -= 8
            reasons.append(f"ROE at {roe:.1f}% — weak returns on equity")

    d2e = f.get("debt_to_equity")
    if d2e is not None:
        if d2e < 50:
            score += 8
            reasons.append(f"Debt/Equity at {d2e:.0f} — low leverage, low risk")
        elif d2e > 150:
            score -= 12
            reasons.append(f"Debt/Equity at {d2e:.0f} — high leverage, elevated risk")
        else:
            reasons.append(f"Debt/Equity at {d2e:.0f} — moderate leverage")

    fcf = f.get("free_cashflow")
    if fcf is not None:
        if fcf > 0:
            score += 8
            reasons.append("Positive free cash flow — self-funding operations and growth")
        else:
            score -= 10
            reasons.append("Negative free cash flow — relies on external financing")

    margins = _pct(f.get("profit_margins"))
    if margins is not None:
        if margins > 15:
            score += 8
            reasons.append(f"Net margin at {margins:.1f}% — strong pricing power/efficiency")
        elif margins < 5:
            score -= 8
            reasons.append(f"Net margin at {margins:.1f}% — thin profitability")

    inst_holding = _pct(f.get("held_percent_institutions"))
    if inst_holding is not None and inst_holding > 40:
        score += 6
        reasons.append(f"Institutions hold {inst_holding:.1f}% — strong smart-money confidence")

    pe = f.get("pe_ratio")
    forward_pe = f.get("forward_pe")
    valuation_note = "fair"
    if pe is not None:
        if pe < 0:
            score -= 10
            valuation_note = "unprofitable (negative P/E)"
            reasons.append("Negative P/E — company currently unprofitable")
        elif pe > 60:
            score -= 8
            valuation_note = "expensive"
            reasons.append(f"P/E at {pe:.1f} — richly valued, needs strong growth to justify")
        elif pe < 15:
            score += 6
            valuation_note = "attractive"
            reasons.append(f"P/E at {pe:.1f} — attractively valued vs typical large-cap range")
        if forward_pe and pe and forward_pe < pe:
            score += 4
            reasons.append("Forward P/E lower than trailing P/E — earnings expected to grow into valuation")

    # FALLBACK: if no reasons were added, add a generic summary
    if not reasons:
        reasons.append("Fundamental data partially available; score is based on available metrics")
        if pe is not None:
            reasons.append(f"P/E ratio is {pe:.1f}")
        if roe is not None:
            reasons.append(f"ROE is {roe:.1f}%")
        if rev_growth is not None:
            reasons.append(f"Revenue growth is {rev_growth:.1f}%")

    score = max(0, min(100, round(score)))

    return {
        "symbol": symbol.upper(),
        "fundamental_score": score,
        "valuation": valuation_note,
        "sector": f.get("sector"),
        "industry": f.get("industry"),
        "reasons": reasons,
        "raw": f,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8003))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)