"""
Notification Service
-----------------------
Single responsibility: deliver an alert when — and only when — something
actionable changed, per the spec's "no unnecessary notifications" rule:
  - A new BUY NOW opportunity appears
  - An existing BUY flips to SELL
  - A tracked event changes a recommendation

Delivery channels are free webhooks — Discord and Slack both offer free
incoming webhooks with no API key/billing account needed; Telegram bots
are also free. Configure whichever you have via env vars; unset channels
are simply skipped.

This service does not decide *what* counts as notification-worthy — the
Scheduler Service (which already tracks previous vs current scan results)
calls POST /notify with a pre-built message. Keeping that decision in the
Scheduler avoids a circular dependency and keeps this service a dumb,
reliable delivery pipe — same design principle as Market Data Service.
"""
import os
import logging

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("notification-service")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

app = FastAPI(title="Stockky Notification Service", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class NotifyRequest(BaseModel):
    title: str
    message: str
    urgency: str = "normal"  # "normal" | "high" — high could map to @here/@channel later


@app.get("/health")
def health():
    channels = {
        "discord": bool(DISCORD_WEBHOOK_URL),
        "slack": bool(SLACK_WEBHOOK_URL),
        "telegram": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    }
    return {"status": "ok", "service": "notification-service", "channels_configured": channels}


def _send_discord(title: str, message: str):
    if not DISCORD_WEBHOOK_URL:
        return None
    payload = {"content": f"**{title}**\n{message}"}
    try:
        resp = httpx.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        return "sent"
    except httpx.HTTPError as e:
        logger.error("Discord notification failed: %s", e)
        return f"failed: {e}"


def _send_slack(title: str, message: str):
    if not SLACK_WEBHOOK_URL:
        return None
    payload = {"text": f"*{title}*\n{message}"}
    try:
        resp = httpx.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        return "sent"
    except httpx.HTTPError as e:
        logger.error("Slack notification failed: %s", e)
        return f"failed: {e}"


def _send_telegram(title: str, message: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"{title}\n{message}"}
    try:
        resp = httpx.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return "sent"
    except httpx.HTTPError as e:
        logger.error("Telegram notification failed: %s", e)
        return f"failed: {e}"


@app.post("/notify")
def notify(req: NotifyRequest):
    results = {
        "discord": _send_discord(req.title, req.message),
        "slack": _send_slack(req.title, req.message),
        "telegram": _send_telegram(req.title, req.message),
    }
    attempted = {k: v for k, v in results.items() if v is not None}
    if not attempted:
        return {
            "delivered": False,
            "note": "No notification channel configured. Set DISCORD_WEBHOOK_URL, SLACK_WEBHOOK_URL, or TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID.",
        }
    return {"delivered": True, "results": attempted}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8008, reload=True)
