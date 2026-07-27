"""
scheduler.py — nightly maintenance.

At local midnight every stored conversation is deleted and every DeepSeek
session pointer is detached, so each day starts clean. MongoDB's TTL index is
the backstop: individual turns also expire 24h after they were written, which
covers the case where the bot was offline at midnight.
"""
import asyncio
import logging
from datetime import datetime, timedelta

import db

log = logging.getLogger("scheduler")


def _seconds_until_midnight(tz=None) -> float:
    now = datetime.now(tz)
    nxt = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max(60.0, (nxt - now).total_seconds())


async def nightly_wipe_loop(bot=None, owner_id: int = 0, tz=None):
    """Runs forever; sleeps until midnight, wipes, repeats."""
    while True:
        wait = _seconds_until_midnight(tz)
        log.info("Next history wipe in %.1f hours", wait / 3600)
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            raise

        try:
            removed = await db.wipe_all_history()
            log.info("Nightly wipe: removed %d stored turns", removed)
            if bot and owner_id:
                try:
                    await bot.send_message(
                        chat_id=owner_id,
                        text=("🧹 <b>Nightly cleanup</b>\n\n"
                              f"Cleared <b>{removed:,}</b> stored conversation "
                              f"turns and reset every DeepSeek session."),
                        parse_mode="HTML", disable_notification=True)
                except Exception:
                    pass
        except Exception as e:
            log.exception("nightly wipe failed: %s", e)
            await asyncio.sleep(300)
