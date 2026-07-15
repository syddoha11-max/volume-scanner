#!/usr/bin/env python3
"""
Binance momentum-continuation scanner ("bull flag" / re-accumulation).

Finds coins that (1) had a real run up, (2) then cooled off in an ORDERLY way --
shallow pullback, holding most of the gains, on FADING volume -- and (3) are now
starting to turn back up. Alerts via Telegram + email (reuses the same secrets).
Daily candles; reads Binance PUBLIC data only.

A coin that gave back most of its run (like a full round-trip) is REJECTED by the
"held above the 50% retracement" filter -- that's a reversal, not a cool-down.
"""

import os
import json
import smtplib
import time
from email.mime.text import MIMEText

import requests

# ------------------------- CONFIG (tweak freely) -------------------------
QUOTE_ASSET       = "USDT"
CANDLE            = "1d"
RUN_LOOKBACK      = 14          # look for the strongest leg within the last N days
MIN_RUN           = 0.30        # the run must be >= 30%
STRONG_RUN        = 0.60        # tag as STRONG at/above 60%
COOLDOWN_MIN_DAYS = 2           # must have cooled for at least this many days...
COOLDOWN_MAX_DAYS = 12          # ...but the run must still be recent
PULLBACK_MIN      = 0.10        # cooled at least 10% off the high...
PULLBACK_MAX      = 0.55        # ...but not collapsed
STRUCT_HOLD       = 0.50        # must still hold above the 50% retracement of the run
VOL_FADE          = 1.0         # avg pullback volume must be below avg run volume
MIN_24H_QUOTE_VOL = 3_000_000   # reasonably liquid only
TOP_N             = 150         # cap coins we pull candles for (rate limit)
COOLDOWN_HOURS    = 18          # don't re-alert the same coin within this window

EXCLUDE_BASES = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "USDC", "FDUSD", "TUSD", "DAI",
    "STETH", "WBETH", "WBTC",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
WATCHLIST: set[str] = set()

STATE_FILE = "state_momentum.json"
BINANCE = "https://data-api.binance.vision"
HTTP_TIMEOUT = 20
# ------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z] {msg}", flush=True)


def session_tag() -> str:
    t = time.gmtime()
    h = t.tm_hour + t.tm_min / 60.0
    if 12.0 <= h < 14.5:
        return "🇺🇸 US open/data window — peak liquidity"
    if 14.5 <= h < 21.0:
        return "🇺🇸 US session — high liquidity"
    if 7.0 <= h < 12.0:
        return "🇪🇺 London/EU session — good liquidity"
    if 0.0 <= h < 2.0:
        return "🌏 Asia open + daily close/funding — active"
    if 2.0 <= h < 7.0:
        return "🌏 Asian session — moderate liquidity"
    return "🌙 Off-hours — thin liquidity, watch for fakeouts"


def get_json(url: str):
    r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "momentum/1.0"})
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
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:TOP_N]]


def analyse(sym: str):
    url = f"{BINANCE}/api/v3/klines?symbol={sym}&interval={CANDLE}&limit={RUN_LOOKBACK + 3}"
    try:
        kl = get_json(url)
    except Exception:
        return None
    if len(kl) < RUN_LOOKBACK + 2:
        return None
    return analyse_klines(sym, kl)


def analyse_klines(sym: str, kl: list):
    """Pure logic (separated so it can be unit-tested offline)."""
    forming = kl[-1]
    closed = kl[:-1]
    window = closed[-RUN_LOOKBACK:]
    if len(window) < 4:
        return None
    try:
        highs = [float(c[2]) for c in window]
        lows = [float(c[3]) for c in window]
        closes = [float(c[4]) for c in window]
        qvols = [float(c[7]) for c in window]
        price = float(forming[4])          # current live price
    except (IndexError, TypeError, ValueError):
        return None

    h_idx = max(range(len(highs)), key=lambda i: highs[i])
    if h_idx < 1:                          # need a low before the high
        return None
    run_high = highs[h_idx]
    pre_low = min(lows[: h_idx + 1])
    if pre_low <= 0:
        return None

    run_pct = (run_high - pre_low) / pre_low
    if run_pct < MIN_RUN:
        return None

    days_since_high = (len(window) - 1) - h_idx
    if not (COOLDOWN_MIN_DAYS <= days_since_high <= COOLDOWN_MAX_DAYS):
        return None

    pullback = (run_high - price) / run_high
    if not (PULLBACK_MIN <= pullback <= PULLBACK_MAX):
        return None

    floor = pre_low + STRUCT_HOLD * (run_high - pre_low)
    if price <= floor:                     # gave back too much = reversal, not cool-down
        return None

    run_vols = qvols[: h_idx + 1]
    pull_vols = qvols[h_idx + 1:]
    if not pull_vols:
        return None
    avg_run = sum(run_vols) / len(run_vols)
    avg_pull = sum(pull_vols) / len(pull_vols)
    if avg_run <= 0 or avg_pull >= VOL_FADE * avg_run:   # volume must have faded
        return None

    # resumption trigger: last closed day up vs the one before, and price holding above it
    if not (closes[-1] > closes[-2] and price >= closes[-1]):
        return None

    return {
        "base": sym[: -len(QUOTE_ASSET)], "sym": sym,
        "run_pct": run_pct * 100, "days": days_since_high,
        "pullback": pullback * 100, "price": price,
        "floor": floor, "target": run_high,
        "vol_fade": avg_pull / avg_run,
        "strong": run_pct >= STRONG_RUN,
    }


def num(n: float) -> str:
    return f"{n:.6g}"


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
    log(f"checking {len(syms)} coins for momentum-continuation setups")

    hits = []
    for sym in syms:
        if now - state.get(sym, 0) < COOLDOWN_HOURS * 3600:
            continue
        res = analyse(sym)
        if res:
            hits.append(res)
        time.sleep(0.15)

    if not hits:
        log("no continuation setups this run")
        save_state(state)
        return

    hits.sort(key=lambda x: x["run_pct"], reverse=True)
    lines = ["\U0001F504 Momentum-continuation setups (resuming)"]
    for h in hits[:15]:
        tag = "💪 STRONG " if h["strong"] else ""
        lines.append(
            f"{tag}{h['base']} — ran +{h['run_pct']:.0f}% (peaked {h['days']}d ago), "
            f"cooled -{h['pullback']:.0f}% on lighter volume, turning up. "
            f"Holding >${num(h['floor'])}; reclaim ${num(h['target'])} for continuation "
            f"(now ${num(h['price'])})."
        )
        state[h["sym"]] = now
    lines.append(f"Session: {session_tag()}")
    lines.append("Continuation is NOT guaranteed — set a stop below the pullback low. "
                 "Info only, not advice.")
    text = "\n".join(lines)

    log(f"ALERT: {len(hits)} setup(s)")
    send_telegram(text)
    send_email(f"Continuation setups: {len(hits)} coin(s)", text)
    save_state(state)


if __name__ == "__main__":
    main()
