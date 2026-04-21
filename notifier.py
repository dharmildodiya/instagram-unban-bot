"""
notifier.py — Telegram notification formatter and sender.

Notification formats:

UNBAN:
  Account Recovered | @username 🏆✅
  Followers: 700,850,486 | Following: 244
  ⏱ Time Taken: 69 hours, 12 minutes, 36 seconds
  Unbanned at 2026-04-21 17:45:22 IST
  https://instagram.com/username

BAN:
  💀 @username is now banned.
  ⏱ Time Taken: 0h 0m 1s
  Banned at 2026-04-21 17:45:33 IST
  https://instagram.com/username
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

import database as db

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


# ── Time helpers ──────────────────────────────────────────────────────────────

def now_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def elapsed_since(iso_ts: Optional[str]) -> int:
    if not iso_ts:
        return 0
    try:
        then = datetime.fromisoformat(iso_ts)
        return max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    except Exception:
        return 0


def fmt_ban_time(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m {s}s"


def fmt_unban_time(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    parts = []
    if h: parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m: parts.append(f"{m} minute{'s' if m != 1 else ''}")
    parts.append(f"{s} second{'s' if s != 1 else ''}")
    return ", ".join(parts)


# ── Message builders ──────────────────────────────────────────────────────────

def build_unban_message(username: str, elapsed: int, stats: Optional[dict]) -> str:
    stats_line = ""
    if stats and stats.get("followers") is not None:
        stats_line = (
            f"<b>Followers:</b> {stats['followers']:,} | "
            f"<b>Following:</b> {stats['following']:,}\n"
        )
    return (
        f"Account Recovered | <b>@{username}</b> 🏆✅\n"
        f"{stats_line}"
        f"⏱ <b>Time Taken:</b> {fmt_unban_time(elapsed)}\n"
        f"Unbanned at {now_ist()}\n"
        f"https://instagram.com/{username}"
    )


def build_ban_message(username: str, elapsed: int) -> str:
    return (
        f"💀 <b>@{username} is now banned.</b>\n"
        f"⏱ <b>Time Taken:</b> {fmt_ban_time(elapsed)}\n"
        f"Banned at {now_ist()}\n"
        f"https://instagram.com/{username}"
    )


# ── Sender ────────────────────────────────────────────────────────────────────

async def broadcast(bot: Bot, message: str, owner_ids: list[int]):
    """Send message to all notify targets."""
    targets: set = set(db.get_admins()) | set(owner_ids)

    for chat_id in targets:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,  # shows IG profile preview
            )
            logger.info("Alert sent to %s", chat_id)
        except TelegramError as e:
            logger.error("Failed to send to %s: %s", chat_id, e)
        except Exception as e:
            logger.error("Unexpected send error to %s: %s", chat_id, e)
