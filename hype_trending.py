#!/usr/bin/env python3
"""
Hype / trending-attention alert.

Uses CoinGecko's free "trending" data -- the coins people are suddenly searching
and buzzing about most (the measurable footprint of celebrity/news/community
hype) -- and cross-checks which of them are tradeable on Binance, with their 24h
move. Runs a few times a day.

HONEST WARNING baked into every alert: trending = crowded = often late and risky.
This is the most dangerous category (pump-and-dumps). It flags attention early;
it does not tell you to buy. Verify before doing anything.
"""

import os, json, smtplib, time
import requests

CG_TRENDING = "https://api.coingecko.com/api/v3/search/trending"
BINANCE = "https://data-api.binance.vision"
STATE_FILE = "state_hype.json"
COOLDOWN_HOURS = 18.0        # don't repeat the same coin within this window
HTTP_TIMEOUT = 25


def log(m): print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z] {m}", flush=True)
def get_json(u):
    r = requests.get(u, timeout=HTTP_TIMEOUT, headers={"User-Agent": "hype/1.0"}); r.raise_for_status(); return r.json()
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
    import smtplib
    from email.mime.text import MIMEText
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
    state = load_state()

    try:
        cg = get_json(CG_TRENDING)
    except Exception as e:
        log(f"fatal: CoinGecko trending fetch failed: {e}"); return

    coins = cg.get("coins", []) if isinstance(cg, dict) else []
    if not coins:
        log("no trending coins returned"); return

    # Binance 24h stats, to see which trending coins are tradeable + their move
    try:
        bt = {r["symbol"]: r for r in get_json(f"{BINANCE}/api/v3/ticker/24hr")}
    except Exception as e:
        log(f"warn: Binance stats fetch failed: {e}"); bt = {}

    hits = []
    for c in coins:
        item = c.get("item", {}) if isinstance(c, dict) else {}
        sym = (item.get("symbol") or "").upper()
        name = item.get("name") or sym
        rank = item.get("market_cap_rank")
        if not sym:
            continue
        bsym = f"{sym}USDT"
        on_binance = bsym in bt
        if now - state.get(bsym, 0) < COOLDOWN_HOURS * 3600:
            continue
        pc24 = None
        if on_binance:
            try: pc24 = float(bt[bsym].get("priceChangePercent", 0) or 0)
            except (TypeError, ValueError): pc24 = None
        hits.append({"sym": sym, "name": name, "rank": rank,
                     "on_binance": on_binance, "pc24": pc24, "bsym": bsym})

    if not hits:
        log("nothing new trending"); save_state(state); return

    lines = ["🔥 Trending / buzz (by search interest)"]
    for h in hits:
        if h["on_binance"]:
            mv = f"{h['pc24']:+.0f}% 24h" if h["pc24"] is not None else ""
            lines.append(f"{h['name']} ({h['sym']}) — on Binance {mv}".rstrip())
        else:
            lines.append(f"{h['name']} ({h['sym']}) — NOT on Binance (can't trade here)")
        state[h["bsym"]] = now
    lines.append("⚠️ Trending = crowded & often LATE. Highest-risk category "
                 "(pump-and-dumps). Verify, don't chase. Not advice.")
    text = "\n".join(lines)

    log(f"ALERT: {len(hits)} trending coin(s)")
    send_telegram(text)
    send_email(f"Trending: {len(hits)} coin(s)", text)
    save_state(state)


if __name__ == "__main__":
    main()
