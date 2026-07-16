#!/usr/bin/env python3
"""
Market-regime ("market weather") alert.

Tells you whether the broad market is RISK-ON (Bitcoin trending up -> breakouts &
continuations more reliable) or RISK-OFF (BTC breaking down -> be cautious).
Only pings when the regime CHANGES, so it's low-noise. Reads Binance public data.
"""

import os, json, smtplib, time
from email.mime.text import MIMEText
import requests

SYMBOL = "BTCUSDT"
FAST, SLOW = 20, 50          # daily SMAs
STATE_FILE = "state_regime.json"
BINANCE = "https://data-api.binance.vision"
HTTP_TIMEOUT = 20


def log(m): print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z] {m}", flush=True)
def get_json(u):
    r = requests.get(u, timeout=HTTP_TIMEOUT, headers={"User-Agent": "regime/1.0"}); r.raise_for_status(); return r.json()
def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception: return {}
def save_state(s):
    try:
        with open(STATE_FILE, "w") as f: json.dump(s, f)
    except Exception as e: log(f"warn: save state {e}")


def regime_from_closes(closes):
    if len(closes) < SLOW + 1:
        return None
    price = closes[-1]
    sma_fast = sum(closes[-FAST:]) / FAST
    sma_slow = sum(closes[-SLOW:]) / SLOW
    if price > sma_fast and sma_fast >= sma_slow:
        return "RISK-ON"
    if price < sma_fast and price < sma_slow:
        return "RISK-OFF"
    return "NEUTRAL"


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        log("telegram: not configured"); return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                      timeout=HTTP_TIMEOUT).raise_for_status()
        log("telegram: sent")
    except Exception as e: log(f"telegram: FAILED {e}")


def send_email(subject, body):
    host = os.environ.get("EMAIL_SMTP_HOST", "").strip(); port = os.environ.get("EMAIL_SMTP_PORT", "").strip()
    user = os.environ.get("EMAIL_USER", "").strip(); pw = os.environ.get("EMAIL_PASS", "").strip()
    to = os.environ.get("EMAIL_TO", "").strip() or user
    if not all([host, port, user, pw]):
        log("email: not configured"); return
    try:
        msg = MIMEText(body); msg["Subject"] = subject; msg["From"] = user; msg["To"] = to
        with smtplib.SMTP(host, int(port), timeout=HTTP_TIMEOUT) as s:
            s.starttls(); s.login(user, pw); s.sendmail(user, [to], msg.as_string())
        log("email: sent")
    except Exception as e: log(f"email: FAILED {e}")


def main():
    state = load_state()
    try:
        kl = get_json(f"{BINANCE}/api/v3/klines?symbol={SYMBOL}&interval=1d&limit={SLOW + 5}")
        closes = [float(c[4]) for c in kl]
    except Exception as e:
        log(f"fatal: {e}"); return

    regime = regime_from_closes(closes)
    if regime is None:
        log("not enough data"); return
    prev = state.get("regime")
    log(f"regime={regime} (prev={prev}) BTC={closes[-1]:.0f}")

    if regime == prev:
        return  # no change, stay quiet

    icon = {"RISK-ON": "🟢", "RISK-OFF": "🔴", "NEUTRAL": "🟡"}[regime]
    note = {
        "RISK-ON":  "BTC is above its 20-day trend. Breakout/continuation setups tend to work better — but confirm each one.",
        "RISK-OFF": "BTC lost its trend. Expect more failed breakouts and sharp reversals — tighten up or sit out.",
        "NEUTRAL":  "BTC is chopping between its trends — mixed conditions, lower conviction.",
    }[regime]
    text = (f"{icon} MARKET REGIME → {regime}\n{note}\nBTC ${closes[-1]:,.0f} "
            f"(20d ${sum(closes[-FAST:])/FAST:,.0f} / 50d ${sum(closes[-SLOW:])/SLOW:,.0f})\n"
            f"Context only, not advice.")
    send_telegram(text)
    send_email(f"Market regime: {regime}", text)
    state["regime"] = regime
    save_state(state)


if __name__ == "__main__":
    main()
