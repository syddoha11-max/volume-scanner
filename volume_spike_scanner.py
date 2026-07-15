#!/usr/bin/env python3
"""
Binance volume-spike scanner (15-minute window).

Alerts (Telegram + email) when a coin's most recent 15-MINUTE trading volume is
running well above its own recent average 15-min volume -- i.e. a fresh surge,
caught early rather than after a full hour. New listings are picked up
automatically. Reads Binance PUBLIC data only; never touches your account.
Secrets come from environment variables (GitHub Actions secrets).

Signal:  ratio = (quote volume in last 15m) / (24h quote volume / 96)
         96 = number of 15-min blocks in a day.
"""

import os
import json
import smtplib
import time
from email.mime.text import MIMEText
from urllib.parse import quote as urlquote

import requests

# ------------------------- CONFIG (tweak freely) -------------------------
QUOTE_ASSET       = "USDT"
WINDOW            = "15m"       # Binance rolling window: "5m","15m","30m","1h"
WINDOW_MINUTES    = 15          # must match WINDOW (5, 15, 30, or 60)
MIN_24H_QUOTE_VOL = 1_000_000   # ignore coins with < $1M 24h volume (illiquid)
MIN_WIN_QUOTE_VOL = 40_000      # the spike window must have real money in it
SPIKE_RATIO       = 3.0         # alert when window volume >= 3x its average
STRONG_RATIO      = 5.0         # mark as STRONG at/above this
COOLDOWN_HOURS    = 2.0         # don't re-alert the same coin within this many hours

EXCLUDE_BASES = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "USDC", "FDUSD", "TUSD", "DAI",
    "STETH", "WBETH", "WBTC",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
WATCHLIST: set[str] = set()     # empty = scan the whole market

STATE_FILE = "state.json"
BINANCE = "https://data-api.binance.vision"   # public data host (no US geo-block)
HTTP_TIMEOUT = 20
PERIODS_PER_DAY = 1440 / WINDOW_MINUTES
# ------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z] {msg}", flush=True)


def get_json(url: str):
    r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "vol-scanner/2.0"})
    r.raise_for_status()
    return r.json()


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log(f"warn: could not save state: {e}")


def fetch_24h_all() -> dict:
    return {row["symbol"]: row for row in get_json(f"{BINANCE}/api/v3/ticker/24hr")}


def fetch_window_batch(symbols: list[str]) -> dict:
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        arr = json.dumps(chunk, separators=(",", ":"))
        url = f"{BINANCE}/api/v3/ticker?windowSize={WINDOW}&symbols={urlquote(arr)}"
        try:
            for row in get_json(url):
                out[row["symbol"]] = row
        except Exception as e:
            log(f"warn: {WINDOW} batch failed ({len(chunk)} symbols): {e}")
        time.sleep(0.3)
    return out


def is_leveraged(base: str, known_bases: set[str]) -> bool:
    for suf in LEVERAGED_SUFFIXES:
        if base.endswith(suf) and base[: -len(suf)] in known_bases:
            return True
    return False


def candidate_symbols(t24: dict) -> list[str]:
    known_bases = {s[: -len(QUOTE_ASSET)] for s in t24 if s.endswith(QUOTE_ASSET)}
    syms = []
    for sym, row in t24.items():
        if not sym.endswith(QUOTE_ASSET):
            continue
        base = sym[: -len(QUOTE_ASSET)]
        if base in EXCLUDE_BASES:
            continue
        if is_leveraged(base, known_bases):
            continue
        if WATCHLIST and base not in WATCHLIST:
            continue
        try:
            if float(row.get("quoteVolume", 0)) < MIN_24H_QUOTE_VOL:
                continue
        except (TypeError, ValueError):
            continue
        syms.append(sym)
    return syms


def human(n: float) -> str:
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n/1_000:.0f}k"
    return f"${n:.0f}"


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log("telegram: not configured, skipping")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        log("telegram: sent")
    except Exception as e:
        log(f"telegram: FAILED {e}")


def send_email(subject: str, body: str) -> None:
    host = os.environ.get("EMAIL_SMTP_HOST", "").strip()
    port = os.environ.get("EMAIL_SMTP_PORT", "").strip()
    user = os.environ.get("EMAIL_USER", "").strip()
    pw   = os.environ.get("EMAIL_PASS", "").strip()
    to   = os.environ.get("EMAIL_TO", "").strip() or user
    if not all([host, port, user, pw]):
        log("email: not configured, skipping")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        with smtplib.SMTP(host, int(port), timeout=HTTP_TIMEOUT) as s:
            s.starttls()
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
        log("email: sent")
    except Exception as e:
        log(f"email: FAILED {e}")


def main() -> None:
    now = time.time()
    state = load_state()
    try:
        t24 = fetch_24h_all()
    except Exception as e:
        log(f"fatal: could not fetch 24h data: {e}")
        return

    cands = candidate_symbols(t24)
    log(f"scanning {len(cands)} liquid {QUOTE_ASSET} pairs ({WINDOW} window)")
    if not cands:
        return

    twin = fetch_window_batch(cands)

    spikes = []
    for sym in cands:
        r24 = t24.get(sym)
        rw = twin.get(sym)
        if not r24 or not rw:
            continue
        try:
            qv24 = float(r24["quoteVolume"])
            qvw = float(rw["quoteVolume"])
        except (KeyError, TypeError, ValueError):
            continue
        if qvw < MIN_WIN_QUOTE_VOL or qv24 <= 0:
            continue
        avg_win = qv24 / PERIODS_PER_DAY
        if avg_win <= 0:
            continue
        ratio = qvw / avg_win
        if ratio < SPIKE_RATIO:
            continue
        if now - state.get(sym, 0) < COOLDOWN_HOURS * 3600:
            continue
        spikes.append({
            "base": sym[: -len(QUOTE_ASSET)], "sym": sym, "ratio": ratio, "qvw": qvw,
            "pcw": float(rw.get("priceChangePercent", 0) or 0),
            "pc24": float(r24.get("priceChangePercent", 0) or 0),
            "last": float(r24.get("lastPrice", 0) or 0),
            "strong": ratio >= STRONG_RATIO,
        })

    spikes.sort(key=lambda x: x["ratio"], reverse=True)
    if not spikes:
        log("no volume spikes this run")
        save_state(state)
        return

    MAX_ALERTS = 15
    shown = spikes[:MAX_ALERTS]
    lines = [f"\U0001F514 Volume spikes ({WINDOW}, newer/small-caps)"]
    for s in shown:
        tag = "⚡ STRONG " if s["strong"] else ""
        lines.append(
            f"{tag}{s['base']} — {s['ratio']:.1f}x normal {WINDOW} vol | "
            f"{WINDOW} vol {human(s['qvw'])} | {WINDOW} {s['pcw']:+.1f}% | "
            f"last ${s['last']:.6g} | 24h {s['pc24']:+.1f}%"
        )
        state[s["sym"]] = now
    if len(spikes) > MAX_ALERTS:
        lines.append(f"...and {len(spikes) - MAX_ALERTS} more")
    lines.append("Info only — your call whether to trade; not financial advice.")
    text = "\n".join(lines)

    log(f"ALERT: {len(spikes)} spike(s)")
    send_telegram(text)
    send_email(f"Volume spikes: {len(spikes)} coin(s)", text)
    save_state(state)


if __name__ == "__main__":
    main()
