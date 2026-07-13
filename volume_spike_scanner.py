#!/usr/bin/env python3
"""
Binance volume-spike scanner.

Checks every liquid Binance USDT spot pair and alerts (Telegram + email) when a
coin's most recent 1-hour trading volume is running well above its own recent
average hourly volume -- i.e. a sudden volume spike. New listings are picked up
automatically because the scanner reads the live symbol list each run.

Nothing here touches your exchange account. It only reads Binance PUBLIC market
data. All secrets are read from environment variables (set as GitHub Actions
secrets) -- none are stored in this file.

Signal:  ratio = (quote volume in last 1h) / (24h quote volume / 24)
         ratio >= SPIKE_RATIO  -> alert;  ratio >= STRONG_RATIO -> flagged STRONG
"""

import os
import json
import smtplib
import time
from email.mime.text import MIMEText
from urllib.parse import quote as urlquote

import requests

# ------------------------- CONFIG (tweak freely) -------------------------
QUOTE_ASSET       = "USDT"     # only scan pairs quoted in this asset
MIN_24H_QUOTE_VOL = 1_000_000  # ignore coins with < $1M 24h volume (illiquid noise)
MIN_1H_QUOTE_VOL  = 150_000    # the spike hour must have real money in it
SPIKE_RATIO       = 3.0        # alert when 1h volume >= 3x the average hourly volume
STRONG_RATIO      = 5.0        # mark as STRONG at/above this
COOLDOWN_HOURS    = 3.0        # don't re-alert the same coin within this many hours

# Skip the mega-caps so alerts skew toward newer / smaller coins (the interesting ones).
# Delete names from this set if you DO want to be alerted on them.
EXCLUDE_BASES = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "USDC", "FDUSD", "TUSD", "DAI",
    "STETH", "WBETH", "WBTC",
}
# Also skip Binance leveraged tokens (e.g. BTCUP, ETHDOWN). Detected precisely
# below so real coins like JUP/PUMP that merely END in "UP" are NOT dropped.
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

# If you'd rather watch ONLY a fixed list instead of the whole market, put base
# symbols here, e.g. WATCHLIST = {"ENA", "WIF", "PENGU"}.  Empty = scan everything.
WATCHLIST: set[str] = set()

STATE_FILE = "state.json"      # remembers last-alert times to enforce the cooldown
BINANCE = "https://api.binance.com"
HTTP_TIMEOUT = 20
# ------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z] {msg}", flush=True)


def get_json(url: str):
    r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "vol-scanner/1.0"})
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
    """All symbols' 24h stats in one call. Returns {symbol: row}."""
    data = get_json(f"{BINANCE}/api/v3/ticker/24hr")
    return {row["symbol"]: row for row in data}


def fetch_1h_batch(symbols: list[str]) -> dict:
    """Rolling 1h stats for up to 100 symbols per call. Returns {symbol: row}."""
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        arr = json.dumps(chunk, separators=(",", ":"))
        url = f"{BINANCE}/api/v3/ticker?windowSize=1h&symbols={urlquote(arr)}"
        try:
            for row in get_json(url):
                out[row["symbol"]] = row
        except Exception as e:
            log(f"warn: 1h batch failed ({len(chunk)} symbols): {e}")
        time.sleep(0.3)  # be gentle on rate limits
    return out


def is_leveraged(base: str, known_bases: set[str]) -> bool:
    """True only for real leveraged tokens like BTCUP/ETHDOWN: the part before
    the UP/DOWN/BULL/BEAR suffix must itself be a traded asset. So JUP ('J'+UP)
    is NOT leveraged, but BTCUP ('BTC'+UP) is."""
    for suf in LEVERAGED_SUFFIXES:
        if base.endswith(suf) and base[: -len(suf)] in known_bases:
            return True
    return False


def candidate_symbols(t24: dict) -> list[str]:
    known_bases = {
        s[: -len(QUOTE_ASSET)] for s in t24 if s.endswith(QUOTE_ASSET)
    }
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
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
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
    host = os.environ.get("EMAIL_SMTP_HOST")
    port = os.environ.get("EMAIL_SMTP_PORT")
    user = os.environ.get("EMAIL_USER")
    pw   = os.environ.get("EMAIL_PASS")
    to   = os.environ.get("EMAIL_TO") or user
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
    log(f"scanning {len(cands)} liquid {QUOTE_ASSET} pairs")
    if not cands:
        log("no candidates; done")
        return

    t1h = fetch_1h_batch(cands)

    spikes = []
    for sym in cands:
        r24 = t24.get(sym)
        r1 = t1h.get(sym)
        if not r24 or not r1:
            continue
        try:
            qv24 = float(r24["quoteVolume"])
            qv1  = float(r1["quoteVolume"])
        except (KeyError, TypeError, ValueError):
            continue
        if qv1 < MIN_1H_QUOTE_VOL or qv24 <= 0:
            continue
        avg_hourly = qv24 / 24.0
        if avg_hourly <= 0:
            continue
        ratio = qv1 / avg_hourly
        if ratio < SPIKE_RATIO:
            continue
        # cooldown: skip if we alerted this coin recently
        last = state.get(sym, 0)
        if now - last < COOLDOWN_HOURS * 3600:
            continue
        spikes.append({
            "sym": sym,
            "base": sym[: -len(QUOTE_ASSET)],
            "ratio": ratio,
            "qv1": qv1,
            "pc1": float(r1.get("priceChangePercent", 0) or 0),
            "pc24": float(r24.get("priceChangePercent", 0) or 0),
            "last": float(r24.get("lastPrice", 0) or 0),
            "strong": ratio >= STRONG_RATIO,
        })

    spikes.sort(key=lambda x: x["ratio"], reverse=True)

    if not spikes:
        log("no volume spikes this run")
        save_state(state)
        return

    lines = ["\U0001F514 Binance volume spikes (newer/small-caps)"]
    for s in spikes:
        tag = "⚡ STRONG " if s["strong"] else ""
        lines.append(
            f"{tag}{s['base']} — {s['ratio']:.1f}x normal hourly vol | "
            f"1h vol {human(s['qv1'])} | 1h {s['pc1']:+.1f}% | "
            f"last ${s['last']:.6g} | 24h {s['pc24']:+.1f}%"
        )
        state[s["sym"]] = now
    lines.append("Info only — your call whether to trade; not financial advice.")
    text = "\n".join(lines)

    log(f"ALERT: {len(spikes)} spike(s)")
    send_telegram(text)
    send_email(f"Volume spikes: {len(spikes)} coin(s)", text)
    save_state(state)


if __name__ == "__main__":
    main()
