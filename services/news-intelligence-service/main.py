"""
News Intelligence Service - GenAI Enhanced
---------------------------
Uses Hugging Face Inference API for sentiment scoring.
"""
import os
import logging
from datetime import datetime, timedelta
from typing import List
from urllib.parse import quote

import feedparser
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("news-intelligence-service")

app = FastAPI(title="Stockky News Intelligence Service", version="0.3.0-genai")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

HF_API_URL = "https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.2"
HF_API_KEY = os.getenv("HF_API_KEY")

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


def _score_headline(title: str) -> float:
    """
    Call Hugging Face Inference API to get sentiment score.
    Returns a float between -1 (negative) and +1 (positive).
    Falls back to 0 if API fails.
    """
    if not HF_API_KEY:
        logger.warning("HF_API_KEY not set; using neutral fallback")
        return 0.0
    try:
        payload = {
            "inputs": f"Classify the sentiment of this stock news headline as positive, negative, or neutral: {title}",
            "parameters": {"max_new_tokens": 10, "temperature": 0.1}
        }
        headers = {
            "Authorization": f"Bearer {HF_API_KEY}",
            "Content-Type": "application/json"
        }
        resp = httpx.post(HF_API_URL, json=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            # Hugging Face returns a list of generated texts; we parse sentiment
            result = data[0]['generated_text'].strip().lower()
            if "positive" in result:
                return 0.8
            elif "negative" in result:
                return -0.8
            else:
                return 0.0
        else:
            logger.warning(f"HF API error: {resp.status_code}")
            return 0.0
    except Exception as e:
        logger.warning(f"HF API call failed: {e}")
        return 0.0


@app.get("/")
def root():
    return {
        "service": "Stockky News Intelligence Service",
        "version": "0.3.0-genai",
        "status": "running",
        "model": "Mistral-7B",
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
    avg_sentiment = sum(s for s, _ in scored) / len(scored)

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