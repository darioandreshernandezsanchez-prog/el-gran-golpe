# El Gran Golpe v1.0 — Telegram odds scanner

A starter Telegram bot that polls a sports-odds API, compares available bookmaker prices with a de-vigged market consensus, filters candidate signals, logs them in SQLite, and sends alerts. It **does not place bets** and does not promise profits.

## What v1 currently does

- Telegram commands: `/start`, `/help`, `/scan`, `/status`, `/results`, `/settle ID won|lost|push`.
- Scheduled scans every `POLL_SECONDS` when `TELEGRAM_CHAT_ID` is configured.
- Scans a configurable list of sports (`SPORT_KEYS`) and markets `h2h,spreads,totals`.
- Requires odds at least 1.70, at least three bookmaker observations for an outcome, and estimated price-vs-consensus difference at least 3% by default.
- Saves candidate picks and manually settled outcomes in SQLite to avoid duplicate alerts and calculate a simple simulated unit ROI.
- Does not make bets or access bookmaker accounts.

## Important limitations

1. The consensus probability is computed from the odds returned by the selected data provider after removing each bookmaker's overround. It is **not an independent predictive model**, and a discrepancy is not proof of a true betting edge.
2. Market availability depends on the provider's subscription and the selected sport. Free plans may cover only a few sports and markets.
3. The first version supports common markets (`h2h`, `spreads`, `totals`). Corners, cards, both teams to score, and player scorers require a provider/plan that exposes those markets plus a market-specific adapter. The bot must not invent those prices.
4. The score `/10` is a heuristic signal score based on the estimated discrepancy, not a calibrated probability of winning. It is not an instruction to stake 10 units.
5. Automatic grading is not included yet; `/settle` records outcomes manually. Test in paper-trading mode before risking money.

## 1. Create the Telegram bot

1. Open Telegram and message `@BotFather`.
2. Send `/newbot`, choose a display name and unique username, then copy the token.
3. Keep the token private. Anyone with it can control your bot.

## 2. Get odds data

Choose a sports odds API provider and create an API key. Check which sports/markets your plan includes and the rate limits/cost per request. The default provider adapter tries the current `api.theoddsapi.com` endpoint and falls back to the common v4 endpoint. If your account uses a different endpoint, set `ODDS_API_BASE_URL` and adjust `fetch_sport()` to its documented response format.

## 3. Run locally

Requires Python 3.11+.

```bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

Fill `TELEGRAM_BOT_TOKEN` and `ODDS_API_KEY` in `.env`. Run:

```bash
python bot.py
```

Open your bot in Telegram and send `/start`, then `/scan` to test manually. For scheduled messages, get your chat ID from the bot's incoming update logs or use a trusted Telegram chat-ID method and set `TELEGRAM_CHAT_ID` in `.env`. Restart the process after editing `.env`.

## 4. Run continuously

Use an always-on worker service such as Render Background Worker, Railway, or a small VPS. Add the environment variables from `.env.example` in the service dashboard. Do not commit `.env` or publish API tokens. SQLite is suitable for a small initial test; for multiple replicas or production, use a managed PostgreSQL database and a scheduler/queue.

A container can be built with:

```bash
docker build -t el-gran-golpe .
docker run --env-file .env -v "$PWD/data:/app/data" -e DB_PATH=/app/data/el_gran_golpe.sqlite3 el-gran-golpe
```

## 5. Safe validation plan

- Run 2–4 weeks in paper mode and log every signal before the event starts.
- Record timestamp, price, market, book, closing price if available, outcome, and provider/API errors.
- Evaluate ROI, yield, sample size, drawdown, and closing-line value; separate pre-match and live signals.
- Do not call a signal “stake 10” until a separately validated staking policy exists. Fixed stakes do not increase the chance of winning.

## Settings

- `MIN_ODDS=1.70`
- `MIN_EDGE=0.03` (3% estimated discrepancy)
- `MIN_BOOKMAKERS=3`
- `POLL_SECONDS=900` (15 minutes; no faster than 5 minutes in this starter)
- `MAX_SPORTS_PER_SCAN=5` (control API usage)
- `SPORT_KEYS=...` (provider sport keys; confirm plan availability)
- `ODDS_REGIONS=eu,uk,us`
- `ODDS_MARKETS=h2h,spreads,totals`

Check provider costs before increasing scan frequency or number of sports. Odds providers and bookmakers may restrict scraping or commercial use; use authorized APIs.
