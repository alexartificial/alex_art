# Telegram Reminder Bot

A personal reminder bot you talk to in natural language. Send it a task like
*"meeting with John tomorrow 5pm"*, tell it when to nag you, and it will keep
pinging until you tap **Done**.

## Features

- Natural-language task entry ("call mom in 2 days evening", "submit taxes friday morning")
- Per-task reminder schedules (e.g. `1d 1h 0` = a day before, an hour before, and at due time)
- Inline buttons: ✅ Done · ⏱ Snooze 30m · 🗑 Delete
- Auto-nag every 30 min after the due time until you confirm done
- Survives restarts — tasks and pending reminders are persisted to SQLite via APScheduler

## 1. Get a bot token

You said you already have one. If anyone else is reading this:
open Telegram → message `@BotFather` → send `/newbot` → follow prompts → copy the token it gives you.

## 2. Run locally

```bash
cd "telegram notif "
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and paste your BOT_TOKEN

# load env and start the bot
export $(grep -v '^#' .env | xargs)   # mac/linux
python bot.py
```

Open Telegram, find your bot (search the username you set with BotFather), tap **Start**.
Send `meeting with John tomorrow 5pm` and follow its prompts.

## 3. Deploy to Railway (free tier, 24/7)

1. Push this folder to a GitHub repo (private is fine).
2. Go to [railway.app](https://railway.app), sign in with GitHub.
3. **New Project → Deploy from GitHub repo** → pick this repo.
4. In the project's **Variables** tab, add:
   - `BOT_TOKEN` = your token
   - `TIMEZONE` = `Asia/Kolkata` (or your IANA tz)
5. Railway auto-detects `Procfile` and runs `worker: python bot.py`. That's it.
6. **Recommended:** add a 1 GB volume and mount it at `/data`, then add
   `DB_PATH=/data/reminders.db` so your tasks survive redeploys.

### Render (alternative)

1. New → **Background Worker**, connect the repo.
2. Build command: `pip install -r requirements.txt`
3. Start command: `python bot.py`
4. Add the env vars above. Add a Render disk and set `DB_PATH` similarly if you want persistence.

## How to use it

| You send | Bot does |
|---|---|
| `meeting with John tomorrow 5pm` | Parses date, asks for reminder schedule |
| `1d 1h 0` (in reply to schedule prompt) | Schedules pings 1 day before, 1 hour before, at due time |
| `default` (in reply to schedule prompt) | Same as above (1d / 1h / at-time) |
| `30m` | Single ping, 30 min before due |
| `/list` | Shows your pending tasks with quick `/done_<id>` and `/del_<id>` shortcuts |
| Tap **✅ Done** on a reminder | Marks task complete, cancels remaining reminders |
| Tap **⏱ Snooze 30m** | Adds another ping in 30 minutes |
| Tap **🗑 Delete** | Removes the task entirely |

If you don't tap anything by the due time, the bot keeps re-pinging every 30 minutes
until you mark it Done or Delete it.

## Files

- `bot.py` — the whole bot (handlers, DB, scheduler)
- `requirements.txt` — pinned deps
- `Procfile` / `runtime.txt` — Railway/Heroku/Render deploy hints
- `.env.example` — template for your secrets
- `reminders.db` — created on first run (gitignored)

## Troubleshooting

- **"Missing BOT_TOKEN" on startup** — your env var didn't load. Check `.env` exists and you exported it.
- **Wrong timezone in reminders** — set `TIMEZONE` to a valid IANA name (e.g. `Asia/Kolkata`, not `IST`).
- **Reminders gone after redeploy** — you didn't attach a persistent volume. See the Railway step above.
- **Bot isn't responding** — check the worker logs in Railway/Render; usually it's a missing env var.
