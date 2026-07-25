"""
Tiny HTTP health server so Render (Web Service) doesn't kill the bot.
Runs alongside the polling bot on the PORT env variable.
"""
import os
import asyncio
import logging
from aiohttp import web

log = logging.getLogger("health")


async def _health(request):
    return web.json_response({
        "status": "ok",
        "service": "deepseek-telegram-bot",
    })


async def start_health_server(port: int = None):
    port = int(port or os.getenv("PORT", "10000"))
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health server on 0.0.0.0:%s", port)
    return runner
