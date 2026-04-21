"""
main.py — Entry point.

Starts:
  1. Telegram bot (polling)
  2. Background scheduler loop (checks all accounts)

Commands:
  /add @username    — monitor an account
  /remove @username — stop monitoring
  /list             — show all monitored accounts + status
  /status @username — instant check
  /addadmin <id>    — add admin
  /removeadmin <id> — remove admin
  /proxies          — show proxy pool status
  /help             — command list
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

import database as db
from checker import check_account, check_accounts_batch, get_profile_stats, STATUS_ACTIVE, STATUS_BANNED
from notifier import broadcast, build_ban_message, build_unban_message, elapsed_since
from proxy_manager import proxy_manager

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs.txt", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
OWNER_IDS      = [int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))    # seconds between full cycles
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

HTML = ParseMode.HTML

# ── Helpers ───────────────────────────────────────────────────────────────────

_IG_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?",
    re.IGNORECASE,
)

def parse_username(text: str) -> str | None:
    text = text.strip()
    m = _IG_URL_RE.search(text)
    if m:
        return m.group(1).lower()
    plain = text.lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9._]{1,30}", plain):
        return plain.lower()
    return None

def parse_all_usernames(text: str) -> list[str]:
    seen, result = set(), []
    for token in text.split():
        u = parse_username(token)
        if u and u not in seen:
            seen.add(u); result.append(u)
    return result

def is_admin(user_id: int) -> bool:
    return db.is_admin(user_id, OWNER_IDS)

def status_emoji(s: str) -> str:
    return {"active": "✅", "banned": "🔴", "unknown": "❓"}.get(s, "❓")

def u_link(username: str) -> str:
    return f'<a href="https://instagram.com/{username}">@{username}</a>'


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ You don't have permission.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👁 <b>Instagram Monitor Bot</b>\n\n"
        "Monitors Instagram accounts and alerts you instantly on ban or unban.\n\n"
        "Use /help for all commands.",
        parse_mode=HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Commands</b>\n\n"
        "<code>/add @username</code> — Start monitoring\n"
        "<code>/add user1 user2</code> — Add multiple at once\n"
        "<code>/remove @username</code> — Stop monitoring\n"
        "<code>/list</code> — Show all monitored accounts\n"
        "<code>/status @username</code> — Instant check\n"
        "<code>/proxies</code> — Proxy pool info\n"
        "<code>/addadmin user_id</code> — Add admin\n"
        "<code>/removeadmin user_id</code> — Remove admin\n\n"
        "🔔 <b>Alerts:</b>\n"
        "  💀 Account banned → alert\n"
        "  🏆✅ Account unbanned → alert",
        parse_mode=HTML,
    )


@admin_only
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/add @username</code> or <code>/add user1 user2 user3</code>",
            parse_mode=HTML
        )
        return

    usernames = parse_all_usernames(" ".join(context.args))
    if not usernames:
        await update.message.reply_text("❌ Couldn't find any valid Instagram username(s).")
        return

    msg = await update.message.reply_text(f"🔍 Checking {len(usernames)} account(s)…")
    lines = []

    for username in usernames:
        added = db.add_account(username, update.effective_user.id)
        status = await check_account(username, timeout=REQUEST_TIMEOUT)
        db.update_status(username, status)

        emoji = status_emoji(status)
        label = {"active": "Active", "banned": "Banned", "unknown": "Unknown"}.get(status, "Unknown")
        tag   = "" if added else " <i>(already monitored)</i>"
        lines.append(f"{emoji} {u_link(username)} — <b>{label}</b>{tag}")

    await msg.edit_text("\n".join(lines), parse_mode=HTML, disable_web_page_preview=True)


@admin_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: <code>/remove @username</code>", parse_mode=HTML)
        return

    username = parse_username(" ".join(context.args))
    if not username:
        await update.message.reply_text("❌ Couldn't parse a valid Instagram username.")
        return

    removed = db.remove_account(username)
    if removed:
        await update.message.reply_text(f"🗑 {u_link(username)} removed.", parse_mode=HTML, disable_web_page_preview=True)
    else:
        await update.message.reply_text(f"ℹ️ {u_link(username)} wasn't being monitored.", parse_mode=HTML, disable_web_page_preview=True)


@admin_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = db.get_all_accounts()
    if not accounts:
        await update.message.reply_text("📭 No accounts being monitored.")
        return

    banned  = [a for a in accounts if a["last_status"] == "banned"]
    active  = [a for a in accounts if a["last_status"] == "active"]
    unknown = [a for a in accounts if a["last_status"] not in ("active", "banned")]

    lines = [f"👁 <b>Monitoring {len(accounts)} account(s)</b>\n"]

    if banned:
        lines.append(f"🔴 <b>Banned ({len(banned)})</b>")
        for a in banned:
            chk = (a["last_checked"] or "")[:16].replace("T", " ")
            lines.append(f"  • {u_link(a['username'])} — <code>{chk} UTC</code>")

    if active:
        lines.append(f"\n✅ <b>Active ({len(active)})</b>")
        for a in active:
            chk = (a["last_checked"] or "")[:16].replace("T", " ")
            lines.append(f"  • {u_link(a['username'])} — <code>{chk} UTC</code>")

    if unknown:
        lines.append(f"\n❓ <b>Unknown ({len(unknown)})</b>")
        for a in unknown:
            lines.append(f"  • {u_link(a['username'])}")

    await update.message.reply_text(
        "\n".join(lines), parse_mode=HTML, disable_web_page_preview=True
    )


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: <code>/status @username</code>", parse_mode=HTML)
        return

    username = parse_username(" ".join(context.args))
    if not username:
        await update.message.reply_text("❌ Couldn't parse a valid Instagram username.")
        return

    msg = await update.message.reply_text(f"🔍 Checking @{username}…")
    try:
        status = await check_account(username, timeout=REQUEST_TIMEOUT)
        label = {
            "active":  "✅ Active — profile is accessible",
            "banned":  "🔴 Banned — profile not found",
            "unknown": "❓ Unknown — try again later",
        }.get(status, "❓ Unknown")
        await msg.edit_text(
            f"{label}\n{u_link(username)}",
            parse_mode=HTML, disable_web_page_preview=True
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error: <code>{e}</code>", parse_mode=HTML)


@admin_only
async def cmd_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = proxy_manager.count
    if count == 0:
        await update.message.reply_text("⚠️ No proxies loaded. Add proxies to <code>proxies.txt</code>.", parse_mode=HTML)
    else:
        await update.message.reply_text(f"🔀 <b>{count}</b> proxies loaded and rotating.", parse_mode=HTML)


@admin_only
async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: <code>/addadmin telegram_user_id</code>", parse_mode=HTML)
        return
    uid = int(context.args[0])
    if db.add_admin(uid):
        await update.message.reply_text(f"✅ User <code>{uid}</code> is now an admin.", parse_mode=HTML)
    else:
        await update.message.reply_text(f"ℹ️ Already an admin.", parse_mode=HTML)


@admin_only
async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: <code>/removeadmin telegram_user_id</code>", parse_mode=HTML)
        return
    uid = int(context.args[0])
    if uid in OWNER_IDS:
        await update.message.reply_text("⛔ Cannot remove an owner.", parse_mode=HTML)
        return
    if db.remove_admin(uid):
        await update.message.reply_text(f"✅ Admin <code>{uid}</code> removed.", parse_mode=HTML)
    else:
        await update.message.reply_text(f"ℹ️ Not an admin.", parse_mode=HTML)


# ── Scheduler loop ────────────────────────────────────────────────────────────

async def scheduler_loop(bot):
    """
    Background loop: checks all accounts every CHECK_INTERVAL seconds.
    Fires ban/unban alerts. Removes account after alert.
    """
    logger.info("Scheduler started — interval: %ds", CHECK_INTERVAL)

    while True:
        try:
            accounts = db.get_all_accounts()
            if not accounts:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            usernames = [a["username"] for a in accounts]
            logger.info("Checking %d account(s)…", len(usernames))

            results = await check_accounts_batch(usernames)

            for username, status in results.items():
                try:
                    db.update_status(username, status)

                    # 💀 BAN ALERT
                    if db.needs_ban_alert(username):
                        elapsed = elapsed_since(db.get_status_changed_at(username))
                        db.mark_ban_alerted(username)
                        msg = build_ban_message(username, elapsed)
                        await broadcast(bot, msg, OWNER_IDS)
                        db.remove_account(username)
                        logger.info("💀 Ban alert sent + removed @%s", username)

                    # 🏆 UNBAN ALERT
                    elif db.needs_unban_alert(username):
                        elapsed = elapsed_since(db.get_status_changed_at(username))
                        stats   = await get_profile_stats(username, timeout=REQUEST_TIMEOUT)
                        db.mark_unban_alerted(username)
                        msg = build_unban_message(username, elapsed, stats)
                        await broadcast(bot, msg, OWNER_IDS)
                        db.remove_account(username)
                        logger.info("🏆 Unban alert sent + removed @%s", username)

                    else:
                        logger.info("@%s → %s (no change)", username, status)

                except Exception as e:
                    logger.error("Error processing @%s: %s", username, e)

        except Exception as e:
            logger.error("Scheduler loop error: %s", e)

        await asyncio.sleep(CHECK_INTERVAL)


# ── Main ──────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    """Called after bot starts — kicks off scheduler."""
    asyncio.create_task(scheduler_loop(app.bot))
    logger.info("🤖 Bot started — owner IDs: %s", OWNER_IDS)


def main():
    if not BOT_TOKEN:
        print("❌  BOT_TOKEN not set. Copy .env.example → .env and fill it in.")
        sys.exit(1)
    if not OWNER_IDS:
        print("⚠️  No OWNER_IDS set — you won't be able to use any commands!")

    db.init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("add",         cmd_add))
    app.add_handler(CommandHandler("unban",       cmd_add))     # alias
    app.add_handler(CommandHandler("remove",      cmd_remove))
    app.add_handler(CommandHandler("ban",         cmd_remove))  # alias
    app.add_handler(CommandHandler("cancel",      cmd_remove))  # alias
    app.add_handler(CommandHandler("list",        cmd_list))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("proxies",     cmd_proxies))
    app.add_handler(CommandHandler("addadmin",    cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))

    logger.info("Starting polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    # Auto-restart on crash
    import time
    while True:
        try:
            main()
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break
        except Exception as e:
            logger.critical("Bot crashed: %s — restarting in 10s…", e)
            time.sleep(10)
