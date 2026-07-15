#!/usr/bin/env python3
"""
Binance consolidation-breakout scanner.

More anticipatory than a volume spike: it finds coins that have been coiling in a
TIGHT price range (a "base") and are just now breaking ABOVE it -- and it only
alerts while the breakout is FRESH (not already extended), to avoid chasing.

Alerts via Telegram + email (reuses the same secrets as the volume scanner).
Reads Binance PUBLIC data only; never touches your account.

Logic per coin (using recent candles):
  base   = the last LOOKBACK closed candles (excluding the current forming one)
  tight  = (base_high - base_low) / base_low <= MAX_BASE_RANGE   (it was coiling)
  fresh breakout = base_high*(1+MARGIN) < price <= base_high*(1+MAX_EXTENSION)
  volume confirm = last closed candle's volume > VOL_CONFIRM x the base average
"""

import os
import json
import smtplib
import time
from email.mime.text import MIMEText

import requests

# ------------------------- CONFIG (tweak freely) -------------------------
QUOTE_ASSET       = "USDT"
CANDLE            = "1h"        # candle size for the base (e.g. "15m","1h","4h")
LOOKBACK          = 20          # how many candles form the "base" (20 x 1h = ~20h)
MAX_BASE_RANGE    = 0.08        # base must be tight: high/low within 8%
BREAKOUT_MARGIN   = 0.005       # price must clear the base high by >0.5%
MAX_EXTENSION     = 0.06        # ...but be <6% above it (still fresh, not chased)
VOL_CONFIRM       = 1.3         # breakout candle vol >= 1.3x the base average
MIN_24H_QUOTE_VOL = 2_000_000   # only reasonably liquid coins
TOP_N             = 150         # cap how many coins we pull candles for (rate limit)
COOLDOWN_HOURS    = 6.0         # don't re-alert same coin within this many hours

EXCLUDE_BASES = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "USDC", "FDUSD", "TUSD", "DAI",
    "STETH", "WBETH", "WBTC",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
WATCHLIST: set[str] = set()

STATE_FILE = "state_breakout.json"
BINANCE = "https://data-api.binance.vision"
HTTP_TIMEOUT = 20
# ------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z] {msg}", flush=True)


def get_json(url: str):
    r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "breakout/1.0"})
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


def is_leveraged(base: str, known_bases: set[str]) -> bool:
    for suf in LEVERAGED_SUFFIXES:
        if base.endswith(suf) and base[: -len(suf)] in known_bases:
            return True
    return False


def candidates(t24: list[dict]) -> list[str]:
    known = {r["symbol"][: -len(QUOTE_ASSET)]
             for r in t24 if r["symbol"].endswith(QUOTE_ASSET)}
    rows = []
    for r in t24:
        sym = r["symbol"]
        if not sym.endswith(QUOTE_ASSET):
            continue
        base = sym[: -len(QUOTE_ASSET)]
        if base in EXCLUDE_BASES or is_leveraged(base, known):
            continue
        if WATCHLIST and base not in WATCHLIST:
            continue
        try:
            qv = float(r.get("quoteVolume", 0))
        except (TypeError, ValueError):
            continue
        if qv < MIN_24H_QUOTE_VOL:
            continue
        rows.append((sym, qv))
    rows.sort(key=lambda x: x[1], reverse=True)   # most liquid first
    return [s for s, _ in rows[:TOP_N]]


def analyse(sym: str):
    """Return a breakout dict if the coin is a fresh breakout, else None."""
    url = f"{BINANCE}/api/v3/klines?symbol={sym}&interval={CANDLE}&limit={LOOKBACK + 2}"
    try:
        kl = get_json(url)
    except Exception:
        return None
    if len(kl) < LOOKBACK + 2:
        return None

    # kl[-1] = current forming candle; kl[-2] = the just-closed "breakout" candle;
    # base = the LOOKBACK closed candles BEFORE the breakout candle.
    base = kl[-(LOOKBACK + 2):-2]
    bc = kl[-2]
    try:
        price = float(kl[-1][4])            # current live price
        bc_close = float(bc[4])             # breakout candle close (confirmed)
        bc_vol = float(bc[7])               # breakout candle quote volume
        highs = [float(c[2]) for c in base]
        lows = [float(c[3]) for c in base]
        base_vols = [float(c[7]) for c in base]
    except (IndexError, TypeError, ValueError):
        return None

    base_high = max(highs)
    base_low = min(lows)
    if base_low <= 0:
        return None

    range_pct = (base_high - base_low) / base_low
    if range_pct > MAX_BASE_RANGE:         # not tight enough = not a coil
        return None

    lo = base_high * (1 + BREAKOUT_MARGIN)
    hi = base_high * (1 + MAX_EXTENSION)
    if not (lo < bc_close <= hi):          # breakout candle must FRESH-close above base
        return None
    if price <= base_high:                 # and price must still be holding above it
        return None

    avg_base_vol = sum(base_vols) / len(base_vols) if base_vols else 0
    if avg_base_vol <= 0 or bc_vol < VOL_CONFIRM * avg_base_vol:
        return None                        # breakout not backed by volume

    return {
        "base": sym[: -len(QUOTE_ASSET)], "sym": sym, "price": price,
        "base_high": base_high, "range_pct": range_pct * 100,
        "above_pct": (price / base_high - 1) * 100,
        "vol_x": bc_vol / avg_base_vol,
    }


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
        t24 = get_json(f"{BINANCE}/api/v3/ticker/24hr")
    except Exception as e:
        log(f"fatal: could not fetch 24h data: {e}")
        return

    syms = candidates(t24)
    log(f"checking {len(syms)} coins for {CANDLE} consolidation breakouts")

    hits = []
    for sym in syms:
        if now - state.get(sym, 0) < COOLDOWN_HOURS * 3600:
            continue
        res = analyse(sym)
        if res:
            hits.append(res)
        time.sleep(0.15)   # gentle on rate limits

    if not hits:
        log("no fresh breakouts this run")
        save_state(state)
        return

    hits.sort(key=lambda x: x["vol_x"], reverse=True)
    lines = [f"\U0001F680 Consolidation breakouts ({CANDLE} base)"]
    for h in hits[:15]:
        lines.append(
            f"{h['base']} — broke a {h['range_pct']:.1f}% base | "
            f"now +{h['above_pct']:.1f}% above it | vol {h['vol_x']:.1f}x base | "
            f"last ${h['price']:.6g}"
        )
        state[h["sym"]] = now
    lines.append("Fresh breakout = watch for a hold/retest. Info only, not advice.")
    text = "\n".join(lines)

    log(f"ALERT: {len(hits)} breakout(s)")
    send_telegram(text)
    send_email(f"Breakouts: {len(hits)} coin(s)", text)
    save_state(state)


if __name__ == "__main__":
    main()
