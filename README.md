# Instagram Monitor Bot v2

Production-grade Telegram bot that monitors Instagram accounts and sends instant ban/unban alerts.

## Setup (5 steps)

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env — add your BOT_TOKEN and OWNER_IDS
```

### 3. Add proxies (required for cloud servers)
Edit `proxies.txt` and add your proxies:
```
http://username:password@ip:port
```

### 4. Run
```bash
python main.py
```

### 5. Add accounts to monitor
In Telegram, send:
```
/add @username
/add user1 user2 user3
```

## Commands

| Command | Description |
|---------|-------------|
| `/add @username` | Start monitoring (shows current status instantly) |
| `/remove @username` | Stop monitoring |
| `/list` | Show all monitored accounts |
| `/status @username` | Instant check |
| `/proxies` | Proxy pool info |
| `/addadmin user_id` | Grant admin |
| `/removeadmin user_id` | Revoke admin |

Aliases: `/unban` = `/add`, `/ban` = `/remove`, `/cancel` = `/remove`

## Alert Format

**Banned:**
```
💀 @username is now banned.
⏱ Time Taken: 0h 0m 1s
Banned at 2026-04-21 17:45:33 IST
https://instagram.com/username
```

**Unbanned:**
```
Account Recovered | @username 🏆✅
Followers: 700,850,486 | Following: 244
⏱ Time Taken: 69 hours, 12 minutes, 36 seconds
Unbanned at 2026-04-21 17:45:22 IST
https://instagram.com/username
```

## Deploy on Railway

1. Push to GitHub
2. Connect repo on railway.app
3. Add Variables: `BOT_TOKEN`, `OWNER_IDS`, `CHECK_INTERVAL`
4. Start Command: `python main.py`

## File Structure

```
├── main.py          — Bot entry point + commands + scheduler
├── checker.py       — Async Instagram status checker
├── notifier.py      — Alert message builder + Telegram sender  
├── proxy_manager.py — Rotating proxy pool
├── database.py      — SQLite persistence
├── proxies.txt      — Your proxy list (never commit)
├── .env             — Your secrets (never commit)
├── requirements.txt
└── README.md
```
