"""
scheduler.py — nightly maintenance.

DEFAULT OFF (v7.7.3): the nightly cleanup no longer runs at all unless the
owner explicitly opts in with NIGHTLY_CLEANUP=1. User request — chats are
NEVER auto-deleted: no midnight wipe, no session resets, no report message.

Even when opted in, the pass is PROTECTED-AWARE (v7.7.1):
  • Admins (role=admin) and daily-active users (seen in the last 24h) are
    NEVER touched — their stored chats and DeepSeek session pointers survive.
  • Inactive users' turns expire via the 24h TTL. Turns written while a user
    was protected carry no expiry; the nightly job backfills one once the
    user has gone quiet, so churned users' history still clears eventually.
  • MongoDB's TTL index remains the backstop if the bot is offline at
    midnight (it only ever touches rows that carry an expires_at — i.e.
    never admins'/daily users' rows).
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta

import db

log = logging.getLogger("scheduler")


def nightly_enabled() -> bool:
    """Nightly cleanup runs ONLY when NIGHTLY_CLEANUP=1 is explicitly set."""
    return os.getenv("NIGHTLY_CLEANUP", "0") == "1"


def _seconds_until_midnight(tz=None) -> float:
    now = datetime.now(tz)
    nxt = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max(60.0, (nxt - now).total_seconds())


async def run_nightly_once(bot=None, owner_id: int = 0) -> dict:
    """One protected cleanup pass. Returns stats for logging/tests."""
    protected = await db.protected_uids()
    backfilled = await db.backfill_turn_expiry(protected)
    removed = await db.wipe_expired_history(protected)
    detached = await db.clear_all_sessions(protected=protected)
    stats = {"protected": len(protected), "removed": removed,
             "backfilled": backfilled, "detached": detached}
    log.info("Nightly cleanup: removed %d stale turns from inactive users "
             "(backfilled %d), detached %d sessions — %d protected users "
             "(admins + daily-active) untouched",
             removed, backfilled, detached, len(protected))
    if bot and owner_id:
        try:
            await bot.send_message(
                chat_id=owner_id,
                text=("🧹 <b>Nightly cleanup</b>\n\n"
                      f"Cleared <b>{removed:,}</b> stale turns from inactive "
                      f"users and reset {detached} old session pointers.\n\n"
                      f"🛡 <b>{len(protected):,}</b> protected users "
                      f"(admins + daily-active) ke chats safe hain — "
                      f"ye kabhi automatic delete nahi hote."),
                parse_mode="HTML", disable_notification=True)
        except Exception:
            pass
    return stats


async def nightly_wipe_loop(bot=None, owner_id: int = 0, tz=None):
    """Runs forever; sleeps until midnight, cleans up (protected-aware), repeats."""
    while True:
        wait = _seconds_until_midnight(tz)
        log.info("Next nightly cleanup in %.1f hours", wait / 3600)
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            raise

        try:
            await run_nightly_once(bot, owner_id)
        except Exception as e:
            log.exception("nightly cleanup failed: %s", e)
            await asyncio.sleep(300)
