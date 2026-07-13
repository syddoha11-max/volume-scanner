# Your Binance volume-spike alerter — setup guide

This runs a scan every ~10 minutes in the cloud (free, even when your computer is
off) and messages you on **Telegram + email** whenever a newer/small-cap Binance
coin's hourly volume spikes to 3× its normal pace or more.

You never connect your Binance account. The scanner only reads Binance's public
market data. Your tokens/passwords live in GitHub's encrypted "Secrets" — the code
never contains them, and I never see them.

Total time: ~20 minutes, one time. Follow in order.

---

## Part 1 — Put the code on GitHub

1. Create a free account at https://github.com (skip if you have one).
2. Click the **+** (top right) → **New repository**.
   - Name: `volume-scanner` (anything is fine)
   - Set it to **Public** (public repos get unlimited free Actions minutes)
   - Check **"Add a README file"**
   - Click **Create repository**.
3. Add the three files from this project. Easiest way, per file:
   - Click **Add file → Create new file**.
   - For the scanner: type `volume_spike_scanner.py` as the name, paste the file
     contents, click **Commit changes**.
   - For the workflow: type `.github/workflows/scan.yml` as the name (typing the
     slashes creates the folders), paste its contents, **Commit changes**.
   - For the state file: create `state.json` with just `{}` inside, **Commit changes**.

---

## Part 2 — Make your Telegram bot (your phone alerts)

1. In Telegram, search for **@BotFather** and open the chat.
2. Send `/newbot`, follow the prompts (give it any name). BotFather replies with a
   **bot token** that looks like `123456789:AAE...` — copy it. This is your
   `TELEGRAM_BOT_TOKEN`.
3. Search for **your new bot** by the username you just chose, open it, and press
   **Start** (or send it "hi"). This lets it message you.
4. Get your **chat ID**: in a browser, open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   (paste your token in place of `<YOUR_TOKEN>`). Look for `"chat":{"id":123456789`
   — that number is your `TELEGRAM_CHAT_ID`.
   (If it's empty, send your bot another message and refresh the page.)

---

## Part 3 — Set up email alerts (Gmail example)

1. Your Gmail needs **2-Step Verification** on: https://myaccount.google.com/security
2. Create an **App Password**: https://myaccount.google.com/apppasswords → pick
   "Mail", name it "scanner", and Google gives you a 16-character password. Copy it.
   (This is NOT your normal Gmail password — it's a limited one just for this.)
3. You'll use these email values:
   - `EMAIL_SMTP_HOST` = `smtp.gmail.com`
   - `EMAIL_SMTP_PORT` = `587`
   - `EMAIL_USER` = your full Gmail address
   - `EMAIL_PASS` = the 16-character app password
   - `EMAIL_TO` = the address you want alerts sent to (can be the same Gmail)

---

## Part 4 — Add your secrets to GitHub

1. In your repo, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret** and add each of these (name, then value):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `EMAIL_SMTP_HOST`  → `smtp.gmail.com`
   - `EMAIL_SMTP_PORT`  → `587`
   - `EMAIL_USER`
   - `EMAIL_PASS`
   - `EMAIL_TO`

(Only fill the channels you want. Telegram-only? Skip the EMAIL_* ones — the script
just skips whatever isn't set.)

---

## Part 5 — Turn it on and test

1. Go to the **Actions** tab. If it asks, click **"I understand my workflows,
   enable them."**
2. Click **volume-spike-scan** → **Run workflow** → **Run workflow** to trigger a
   run right now instead of waiting.
3. Open the run to watch the log. You'll see `scanning N liquid USDT pairs`. If any
   coin is spiking, you'll get a Telegram message and email within a minute. If
   none are spiking, that's normal — it just logs "no volume spikes this run".
4. From now on it runs automatically every ~10 minutes.

**Tip:** to force a test alert, temporarily open `volume_spike_scanner.py`, change
`SPIKE_RATIO = 3.0` to `SPIKE_RATIO = 1.1`, commit, run once (you should get pinged),
then change it back to `3.0`.

---

## Tuning (all at the top of `volume_spike_scanner.py`)

- `SPIKE_RATIO` — how big a spike triggers an alert (3× default; raise to 4–5 for
  fewer, stronger alerts).
- `MIN_24H_QUOTE_VOL` / `MIN_1H_QUOTE_VOL` — liquidity floors; raise them to ignore
  smaller coins.
- `COOLDOWN_HOURS` — how long before the same coin can alert again (stops spam).
- `EXCLUDE_BASES` — big coins to ignore. Remove names to include them.
- `WATCHLIST` — leave empty to scan the whole market (auto-catches new listings), or
  set e.g. `{"ENA", "WIF", "PENGU"}` to watch only those.

---

## Notes & limits

- GitHub's free scheduled runs are "best effort" and can be delayed a few minutes
  under load — fine for this use, not for split-second trading.
- GitHub disables scheduled workflows after 60 days of no repo activity; just commit
  any small change occasionally (the state.json commits each alert help keep it
  active).
- This reports the volume signal only. It does not tell you to buy or sell — that
  decision is yours. Not financial advice.
