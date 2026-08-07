"""
News Intelligence Service
---------------------------
Single responsibility: pull recent news for a company from free, keyless
sources (Google News RSS) and turn it into a news sentiment score (0-100)
plus the headlines behind it. The Decision Engine treats this as one more
input score, exactly like Technical/Fundamental.

Why VADER instead of a transformer model: VADER (`vaderSentiment`) is a
tiny, pure-Python lexicon scorer — no multi-GB model download, no GPU,
starts in milliseconds, and free forever. It's a deliberate MVP trade-off:
good enough to separate "clearly bad news" from "clearly good news" on
short headlines. Swap in a `transformers` pipeline
(`distilbert-base-uncased-finetuned-sst-2-english`, also free) later if you
want finer-grained accuracy — the `_score_headline` function is the only
place that needs to change.
"""
import os
import logging
import re
from datetime import datetime, timedelta
from typing import List
from urllib.parse import quote

import feedparser
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("news-intelligence-service")

app = FastAPI(title="Stockky News Intelligence Service", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

analyzer = SentimentIntensityAnalyzer()

# Words that matter a lot more than generic positive/negative sentiment for
# equity news specifically — boost/penalize on top of the base VADER score.
HIGH_IMPACT_NEGATIVE = [
    "fraud", "scam", "raid", "probe", "sebi action", "resignation", "resigns",
    "downgrade", "default", "insolvency", "bankruptcy", "lawsuit", "penalty",
    "fine imposed", "accounting irregularities", "auditor resigns", "ban",
]
HIGH_IMPACT_POSITIVE = [
    "beats estimates", "record profit", "record revenue", "upgrade", "bags order",
    "wins contract", "expansion", "buyback", "bonus issue", "stake acquisition",
    "partnership", "new plant", "capacity expansion",
]

NAME_HINTS = {
    "TCS": "Tata Consultancy Services",
    "INFY": "Infosys",
    "HDFCBANK": "HDFC Bank",
    "ICICIBANK": "ICICI Bank",
    "RELIANCE": "Reliance Industries",
    "HCLTECH": "HCL Technologies",
    "COFORGE": "Coforge",
    "ANGELONE": "Angel One",
    "ADANIPOWER": "Adani Power",
    "BEL": "Bharat Electronics",
    "HAL": "Hindustan Aeronautics",
    "TATAMOTORS": "Tata Motors",
    "SBIN": "State Bank of India",
}


def _company_query(symbol: str) -> str:
    base = symbol.replace(".NS", "").replace(".BO", "").upper()
    return NAME_HINTS.get(base, base) + " NSE stock"


def _score_headline(title: str) -> float:
    base = analyzer.polarity_scores(title)["compound"]  # -1 .. +1
    lowered = title.lower()
    if any(term in lowered for term in HIGH_IMPACT_NEGATIVE):
        base -= 0.6
    if any(term in lowered for term in HIGH_IMPACT_POSITIVE):
        base += 0.4
    return max(-1.0, min(1.0, base))


def _fetch_headlines(symbol: str, max_items: int = 12) -> List[dict]:
    query = quote(_company_query(symbol))
    feed_url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    parsed = feedparser.parse(feed_url)

    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise HTTPException(status_code=502, detail=f"Could not reach news feed for {symbol}")

    cutoff = datetime.utcnow() - timedelta(days=10)
    items = []
    for entry in parsed.entries[:max_items]:
        published = None
        if getattr(entry, "published_parsed", None):
            published = datetime(*entry.published_parsed[:6])
        if published and published < cutoff:
            continue
        items.append({
            "title": entry.title,
            "source": getattr(entry, "source", {}).get("title") if hasattr(entry, "source") else None,
            "published": published.isoformat() if published else None,
            "link": entry.link,
        })
    return items


@app.get("/")
def root():
    return {
        "service": "Stockky News Intelligence Service",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/analyze/{symbol}": "GET – news sentiment score for a symbol",
            "/docs": "Swagger UI documentation",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "news-intelligence-service"}


@app.get("/analyze/{symbol}")
def analyze(symbol: str):
    headlines = _fetch_headlines(symbol)

    if not headlines:
        return {
            "symbol": symbol.upper(),
            "news_score": 50,
            "headline_count": 0,
            "reasons": ["No recent news found — treating as neutral, not a signal either way"],
            "headlines": [],
        }

    scored = [(_score_headline(h["title"]), h) for h in headlines]
    avg_sentiment = sum(s for s, _ in scored) / len(scored)  # -1 .. +1

    # Map -1..+1 sentiment to 0..100 score
    news_score = round((avg_sentiment + 1) * 50)
    news_score = max(0, min(100, news_score))

    reasons = []
    most_positive = max(scored, key=lambda x: x[0])
    most_negative = min(scored, key=lambda x: x[0])

    if most_negative[0] < -0.3:
        reasons.append(f"Notably negative headline: \"{most_negative[1]['title'][:90]}\"")
    if most_positive[0] > 0.3:
        reasons.append(f"Notably positive headline: \"{most_positive[1]['title'][:90]}\"")
    reasons.append(f"{len(headlines)} recent headlines, average sentiment {'positive' if avg_sentiment > 0.1 else 'negative' if avg_sentiment < -0.1 else 'neutral'}")

    return {
        "symbol": symbol.upper(),
        "news_score": news_score,
        "headline_count": len(headlines),
        "reasons": reasons,
        "headlines": [h for _, h in scored[:6]],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8005))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)