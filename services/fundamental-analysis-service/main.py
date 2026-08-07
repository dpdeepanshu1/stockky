"""
Fundamental Analysis Service
------------------------------
Single responsibility: turn raw fundamental data (fetched from Market Data
Service) into a fundamental quality score (0-100) and readable reasons.
"""
import os
import logging

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fundamental-analysis-service")

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://market-data-service.onrender.com")

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

    # Extract metrics (using the same keys as market-data-service)
    rev_growth = f.get("revenue_growth")
    earnings_growth = f.get("earnings_growth")
    roe = f.get("roe")
    d2e = f.get("debt_to_equity")
    fcf = f.get("free_cashflow")
    margins = f.get("profit_margins")
    inst_holding = f.get("held_percent_institutions")
    pe = f.get("pe_ratio")
    forward_pe = f.get("forward_pe")

    # Build metrics dict
    metrics = {
        "revenue_growth": rev_growth,
        "earnings_growth": earnings_growth,
        "roe": roe,
        "debt_to_equity": d2e,
        "free_cashflow": fcf,
        "profit_margins": margins,
        "institutional_holding": inst_holding,
        "pe_ratio": pe,
        "forward_pe": forward_pe,
    }

    # Scoring and reasons
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

    if earnings_growth is not None:
        if earnings_growth > 15:
            score += 12
            reasons.append(f"Earnings growing {earnings_growth:.1f}% YoY — profitable expansion")
        elif earnings_growth < 0:
            score -= 12
            reasons.append(f"Earnings declining {earnings_growth:.1f}% YoY — margin or demand pressure")

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

    if d2e is not None:
        if d2e < 50:
            score += 8
            reasons.append(f"Debt/Equity at {d2e:.0f} — low leverage, low risk")
        elif d2e > 150:
            score -= 12
            reasons.append(f"Debt/Equity at {d2e:.0f} — high leverage, elevated risk")
        else:
            reasons.append(f"Debt/Equity at {d2e:.0f} — moderate leverage")

    if fcf is not None:
        if fcf > 0:
            score += 8
            reasons.append("Positive free cash flow — self-funding operations and growth")
        else:
            score -= 10
            reasons.append("Negative free cash flow — relies on external financing")

    if margins is not None:
        if margins > 15:
            score += 8
            reasons.append(f"Net margin at {margins:.1f}% — strong pricing power/efficiency")
        elif margins < 5:
            score -= 8
            reasons.append(f"Net margin at {margins:.1f}% — thin profitability")

    if inst_holding is not None and inst_holding > 40:
        score += 6
        reasons.append(f"Institutions hold {inst_holding:.1f}% — strong smart-money confidence")

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

    if not reasons:
        reasons.append("Fundamental data partially available; score is based on available metrics")

    score = max(0, min(100, round(score)))

    return {
        "symbol": symbol.upper(),
        "fundamental_score": score,
        "valuation": valuation_note,
        "sector": f.get("sector"),
        "industry": f.get("industry"),
        "reasons": reasons,
        "metrics": metrics,          # <-- KEY: include raw metrics
        "raw": f,
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8003))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)