#!/usr/bin/env python3
"""
Exit / stop-guard alert.

Watches the coins YOU hold and pings you to protect gains: when price hits a stop
you set, or falls a set % from its high while you've been holding (a trailing
stop), or drops sharply in the last hour. Reads Binance public data only -- it
NEVER places orders; it just tells you to look. Fill in HOLDINGS below.
"""

import os, json, smtplib, time
from email.mime.text import MIMEText
from urllib.parse import quote as urlquote
import requests

# ------------- EDIT THIS: the coins you hold and how to guard them -------------
# For each: "stop" = a hard price you want to be warned at (optional),
#           "trail_pct" = warn if it falls this % from its high since you added it
#           (optional, e.g. 0.12 = 12%). You can set either, both, or neither.
HOLDINGS = {
    # "SUIUSDT":  {"stop": 3.20, "trail_pct": 0.12},
    # "ENAUSDT":  {"trail_pct": 0.15},
    # "ONDOUSDT": {"stop": 0.85},
}
FAST_DROP_1H = 0.08          # also warn if any holding drops >8% in the last hour
COOLDOWN_HOURS = 6.0         # don't repeat the same warning within this window
# -----------------------------------------------------------------------------

STATE_FILE = "state_guard.json"
BINANCE = "https://data-api.binance.vision"
HTTP_TIMEOUT = 20


def log(m): print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z] {m}", flush=True)
def get_json(u):
    r = requests.get(u, timeout=HTTP_TIMEOUT, headers={"User-Agent": "guard/1.0"}); r.raise_for_status(); return r.json()
def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception: return {}
def save_state(s):
    try:
        with open(STATE_FILE, "w") as f: json.dump(s, f)
    except Exception as e: log(f"warn: save state {e}")


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(); chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat: log("telegram: not configured"); return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                      timeout=HTTP_TIMEOUT).raise_for_status(); log("telegram: sent")
    except Exception as e: log(f"telegram: FAILED {e}")


def send_email(subject, body):
    host = os.environ.get("EMAIL_SMTP_HOST", "").strip(); port = os.environ.get("EMAIL_SMTP_PORT", "").strip()
    user = os.environ.get("EMAIL_USER", "").strip(); pw = os.environ.get("EMAIL_PASS", "").strip()
    to = os.environ.get("EMAIL_TO", "").strip() or user
    if not all([host, port, user, pw]): log("email: not configured"); return
    try:
        msg = MIMEText(body); msg["Subject"] = subject; msg["From"] = user; msg["To"] = to
        with smtplib.SMTP(host, int(port), timeout=HTTP_TIMEOUT) as s:
            s.starttls(); s.login(user, pw); s.sendmail(user, [to], msg.as_string())
        log("email: sent")
    except Exception as e: log(f"email: FAILED {e}")


def main():
    now = time.time()
    if not HOLDINGS:
        log("no holdings configured — edit HOLDINGS in holdings_guard.py"); return
    state = load_state()
    syms = list(HOLDINGS.keys())

    try:
        arr = urlquote(json.dumps(syms, separators=(",", ":")))
        t24 = {r["symbol"]: r for r in get_json(f"{BINANCE}/api/v3/ticker/24hr?symbols={arr}")}
        t1h = {r["symbol"]: r for r in get_json(f"{BINANCE}/api/v3/ticker?windowSize=1h&symbols={arr}")}
    except Exception as e:
        log(f"fatal: {e}"); return

    warnings = []
    for sym, cfg in HOLDINGS.items():
        r = t24.get(sym)
        if not r:
            log(f"warn: {sym} not found on Binance"); continue
        price = float(r["lastPrice"])
        st = state.get(sym, {})
        high = max(st.get("high", 0.0), price)      # running high since we've watched it
        st["high"] = high
        reasons = []

        stop = cfg.get("stop")
        if stop and price <= float(stop):
            reasons.append(f"hit your stop ${stop} (now ${price:.6g})")

        tp = cfg.get("trail_pct")
        if tp and high > 0 and price <= high * (1 - float(tp)):
            reasons.append(f"down {(1-price/high)*100:.0f}% from its ${high:.6g} high (trail {int(float(tp)*100)}%)")

        r1 = t1h.get(sym)
        if r1:
            pc1 = float(r1.get("priceChangePercent", 0) or 0)
            if pc1 <= -FAST_DROP_1H * 100:
                reasons.append(f"dropped {pc1:.0f}% in the last hour")

        if reasons and now - st.get("last_alert", 0) >= COOLDOWN_HOURS * 3600:
            warnings.append(f"{sym[:-4]} — " + "; ".join(reasons) + f" | last ${price:.6g}")
            st["last_alert"] = now
        state[sym] = st

    if not warnings:
        log("holdings ok, no warnings"); save_state(state); return

    text = "⚠️ EXIT WATCH\n" + "\n".join(warnings) + \
           "\nYour holdings need a look. Info only — you decide; not advice."
    log(f"ALERT: {len(warnings)} holding warning(s)")
    send_telegram(text)
    send_email(f"Exit watch: {len(warnings)} holding(s)", text)
    save_state(state)


if __name__ == "__main__":
    main()
