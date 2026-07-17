#!/usr/bin/env python3
"""
Binance volume-spike scanner (15-min window, escalation-aware).

Alerts when a coin's last-15-min volume runs well above its own average 15-min
volume. First ping = a fresh spike. Then it sends a "🔥 BUILDING" follow-up ONLY
when the SAME coin's spike is clearly growing (volume ratio jumped >= ESCALATION_MULT
and price is higher) -- confirmation the move is real, not a one-print fakeout --
capped per episode so it can't spam. Scans the WHOLE liquid Binance spot USDT
market and auto-includes new listings. Reads public data only.

State is restored/saved via GitHub Actions cache (see workflow) -- NOT git commits
-- so it stays reliable even when triggered every couple of minutes.
"""

import os, json, smtplib, time
from email.mime.text import MIMEText
from urllib.parse import quote as urlquote
import requests

# ------------------------- CONFIG (tweak freely) -------------------------
QUOTE_ASSET       = "USDT"
WINDOW            = "5m"        # 5-min window matches the ~5-min scan cadence (more responsive)
WINDOW_MINUTES    = 5           # must match WINDOW (5, 15, 30, or 60)
MIN_24H_QUOTE_VOL = 500_000     # lowered to include more small-caps (raise to cut noise)
MIN_WIN_QUOTE_VOL = 30_000      # the 5-min window must still have real money in it
SPIKE_RATIO       = 4.0         # first-alert threshold (raise to 5-6 if too chatty)
STRONG_RATIO      = 6.0         # tag first alerts as STRONG at/above this
ESCALATION_MULT   = 1.5         # a follow-up needs ratio >= 1.5x the last alert's ratio
EPISODE_WINDOW_MIN = 45         # follow-ups only within this long after the first ping
MAX_PER_EPISODE   = 3           # first ping + up to 2 "BUILDING" follow-ups
COOLDOWN_HOURS    = 4.0         # after that quiet, a coin can start a fresh episode

EXCLUDE_BASES = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "USDC", "FDUSD", "TUSD", "DAI",
    "STETH", "WBETH", "WBTC",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
WATCHLIST: set[str] = set()

STATE_FILE = "state.json"
BINANCE = "https://data-api.binance.vision"
HTTP_TIMEOUT = 20
PERIODS_PER_DAY = 1440 / WINDOW_MINUTES
# ------------------------------------------------------------------------


def log(m): print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z] {m}", flush=True)


def session_tag() -> str:
    t = time.gmtime(); h = t.tm_hour + t.tm_min / 60.0
    if 12.0 <= h < 14.5: return "🇺🇸 US open/data window — peak liquidity"
    if 14.5 <= h < 21.0: return "🇺🇸 US session — high liquidity"
    if 7.0 <= h < 12.0:  return "🇪🇺 London/EU session — good liquidity"
    if 0.0 <= h < 2.0:   return "🌏 Asia open + daily close/funding — active"
    if 2.0 <= h < 7.0:   return "🌏 Asian session — moderate liquidity"
    return "🌙 Off-hours — thin liquidity, watch for fakeouts"


def get_json(u):
    r = requests.get(u, timeout=HTTP_TIMEOUT, headers={"User-Agent": "vol-scanner/3.0"}); r.raise_for_status(); return r.json()
def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception: return {}
def save_state(s):
    try:
        with open(STATE_FILE, "w") as f: json.dump(s, f)
    except Exception as e: log(f"warn: save state {e}")


def fetch_24h_all(): return {r["symbol"]: r for r in get_json(f"{BINANCE}/api/v3/ticker/24hr")}
def fetch_window_batch(symbols):
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i+100]; arr = json.dumps(chunk, separators=(",", ":"))
        try:
            for r in get_json(f"{BINANCE}/api/v3/ticker?windowSize={WINDOW}&symbols={urlquote(arr)}"):
                out[r["symbol"]] = r
        except Exception as e: log(f"warn: {WINDOW} batch failed: {e}")
        time.sleep(0.3)
    return out


def is_leveraged(base, known):
    for suf in LEVERAGED_SUFFIXES:
        if base.endswith(suf) and base[:-len(suf)] in known: return True
    return False


def candidate_symbols(t24):
    known = {s[:-len(QUOTE_ASSET)] for s in t24 if s.endswith(QUOTE_ASSET)}
    syms = []
    for sym, row in t24.items():
        if not sym.endswith(QUOTE_ASSET): continue
        base = sym[:-len(QUOTE_ASSET)]
        if base in EXCLUDE_BASES or is_leveraged(base, known): continue
        if WATCHLIST and base not in WATCHLIST: continue
        try:
            if float(row.get("quoteVolume", 0)) < MIN_24H_QUOTE_VOL: continue
        except (TypeError, ValueError): continue
        syms.append(sym)
    return syms


def classify(ratio, price, s, now):
    """Decide if this spiking coin is a NEW spike, a BUILDING follow-up, or quiet.
    Returns (kind, new_state_entry) where kind is 'new' | 'building' | None."""
    if not isinstance(s, dict):          # ignore old-format / stale memory, start fresh
        s = None
    if s is None or now - s.get("last_ts", 0) > COOLDOWN_HOURS * 3600:
        return "new", {"first_ts": now, "last_ts": now, "last_ratio": ratio,
                       "peak_price": price, "count": 1}
    building = (now - s.get("first_ts", now) <= EPISODE_WINDOW_MIN * 60
                and s.get("count", 0) < MAX_PER_EPISODE
                and ratio >= ESCALATION_MULT * s.get("last_ratio", ratio)
                and price > s.get("peak_price", 0))
    if building:
        e = dict(s)
        e["prev_ratio"] = s.get("last_ratio"); e["prev_price"] = s.get("peak_price")
        e["last_ts"] = now; e["last_ratio"] = ratio; e["peak_price"] = price
        e["count"] = s.get("count", 0) + 1
        return "building", e
    return None, s          # elevated but not escalating -> stay quiet, don't reset


def human(n):
    if n >= 1_000_000: return f"${n/1_000_000:.1f}M"
    if n >= 1_000: return f"${n/1_000:.0f}k"
    return f"${n:.0f}"


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
    now = time.time(); state = load_state()
    try:
        t24 = fetch_24h_all()
    except Exception as e:
        log(f"fatal: could not fetch 24h data: {e}"); return
    cands = candidate_symbols(t24)
    log(f"scanning {len(cands)} liquid {QUOTE_ASSET} pairs ({WINDOW} window)")
    if not cands: return
    twin = fetch_window_batch(cands)

    new_hits, building_hits = [], []
    for sym in cands:
        r24 = t24.get(sym); rw = twin.get(sym)
        if not r24 or not rw: continue
        try:
            qv24 = float(r24["quoteVolume"]); qvw = float(rw["quoteVolume"])
        except (KeyError, TypeError, ValueError): continue
        if qvw < MIN_WIN_QUOTE_VOL or qv24 <= 0: continue
        avg = qv24 / PERIODS_PER_DAY
        if avg <= 0: continue
        ratio = qvw / avg
        if ratio < SPIKE_RATIO: continue
        price = float(r24.get("lastPrice", 0) or 0)
        kind, entry = classify(ratio, price, state.get(sym), now)
        if kind is None:
            continue
        rec = {"base": sym[:-len(QUOTE_ASSET)], "ratio": ratio, "qvw": qvw,
               "pcw": float(rw.get("priceChangePercent", 0) or 0),
               "pc24": float(r24.get("priceChangePercent", 0) or 0),
               "last": price, "entry": entry, "sym": sym,
               "strong": ratio >= STRONG_RATIO}
        (building_hits if kind == "building" else new_hits).append(rec)
        state[sym] = entry

    if not new_hits and not building_hits:
        log("no volume spikes this run"); save_state(state); return

    new_hits.sort(key=lambda x: x["ratio"], reverse=True)
    building_hits.sort(key=lambda x: x["ratio"], reverse=True)
    lines = []
    if building_hits:
        lines.append("🔥 BUILDING (spike growing)")
        for h in building_hits[:10]:
            e = h["entry"]; since = ((h["last"]/e["prev_price"] - 1) * 100) if e.get("prev_price") else 0
            lines.append(f"{h['base']} — now {h['ratio']:.1f}x (was {e.get('prev_ratio',0):.1f}x) | "
                         f"price {since:+.1f}% since | {WINDOW} vol {human(h['qvw'])} | last ${h['last']:.6g}")
    if new_hits:
        lines.append(f"🔔 New spikes ({WINDOW})")
        for h in new_hits[:12]:
            tag = "⚡ STRONG " if h["strong"] else ""
            lines.append(f"{tag}{h['base']} — {h['ratio']:.1f}x {WINDOW} vol | {human(h['qvw'])} | "
                         f"{WINDOW} {h['pcw']:+.1f}% | last ${h['last']:.6g} | 24h {h['pc24']:+.1f}%")
    lines.append(f"Session: {session_tag()}")
    lines.append("Info only — your call whether to trade; not financial advice.")
    text = "\n".join(lines)

    log(f"ALERT: {len(new_hits)} new, {len(building_hits)} building")
    send_telegram(text)
    send_email(f"Spikes: {len(new_hits)} new / {len(building_hits)} building", text)
    save_state(state)


if __name__ == "__main__":
    main()
