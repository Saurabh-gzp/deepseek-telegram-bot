"""
token_pool.py — DeepSeek key pool with hard concurrency control.

The rule the pool enforces: one DeepSeek key can serve exactly one request at
a time. So N keys == N users talking to DeepSeek simultaneously; with 1 key
the bot answers one person at a time and everyone else waits in line.

    async with POOL.acquire(uid) as lease:
        if lease is None:
            ...  # pool is empty (no keys configured)
        for ev in lease.client.chat_stream(...):
            ...

Leases are strictly scoped: the client object handed out is bound to one key
and returned to the pool the moment the block exits, even on exception.
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import db
from deepseek_client import DeepSeekClient

log = logging.getLogger("pool")


class Lease:
    """A single checked-out key plus its ready-to-use client."""

    def __init__(self, label: str, token: str, client: DeepSeekClient):
        self.label = label
        self.token = token
        self.client = client
        self.acquired_at = time.monotonic()


class TokenPool:
    def __init__(self, workdir: str = "."):
        self.workdir = workdir
        self._clients: Dict[str, DeepSeekClient] = {}   # label -> client
        self._free: Optional[asyncio.Queue] = None      # queue of labels
        self._labels: List[str] = []
        self._tokens: Dict[str, str] = {}               # label -> token
        self._busy: Dict[str, int] = {}                 # label -> uid
        self._lock = asyncio.Lock()

    # ---------- lifecycle ----------

    async def reload(self) -> int:
        """(Re)build the pool from the tokens collection. Returns pool size."""
        async with self._lock:
            rows = await db.list_tokens()
            healthy = [r for r in rows if r.get("healthy", True)]

            self._labels = [r["label"] for r in healthy]
            self._tokens = {r["label"]: r["token"] for r in healthy}

            # keep existing clients so we don't re-download the WASM each time
            for label in list(self._clients):
                if label not in self._tokens:
                    self._clients.pop(label, None)
            for label, token in self._tokens.items():
                existing = self._clients.get(label)
                if existing is None or existing.token != token:
                    self._clients[label] = DeepSeekClient(token,
                                                          workdir=self.workdir)

            q: asyncio.Queue = asyncio.Queue()
            for label in self._labels:
                # don't hand back a key that is mid-request
                if label not in self._busy:
                    q.put_nowait(label)
            self._free = q
            log.info("Token pool reloaded: %d key(s), %d free",
                     len(self._labels), q.qsize())
            return len(self._labels)

    # ---------- introspection ----------

    @property
    def size(self) -> int:
        return len(self._labels)

    @property
    def free_count(self) -> int:
        return self._free.qsize() if self._free else 0

    @property
    def busy_count(self) -> int:
        return len(self._busy)

    def busy_map(self) -> Dict[str, int]:
        return dict(self._busy)

    # ---------- leasing ----------

    @asynccontextmanager
    async def acquire(self, uid: int, timeout: Optional[float] = None):
        """
        Check out a key. Blocks until one is free (or `timeout` seconds).

        Yields a Lease, or None when the pool has no keys at all / the wait
        timed out — callers must handle that case.
        """
        if self._free is None:
            await self.reload()
        if not self._labels:
            yield None
            return

        label = None
        try:
            if timeout is None:
                label = await self._free.get()
            else:
                label = await asyncio.wait_for(self._free.get(), timeout)
        except asyncio.TimeoutError:
            yield None
            return

        self._busy[label] = uid
        try:
            await db.bump_token_use(label)
            yield Lease(label, self._tokens[label], self._clients[label])
        finally:
            self._busy.pop(label, None)
            # only return it if the key still exists after a concurrent reload
            if label in self._tokens and self._free is not None:
                self._free.put_nowait(label)

    async def would_wait(self) -> bool:
        """True if every key is currently in use (so the caller will queue)."""
        return self.size > 0 and self.free_count == 0


POOL = TokenPool()
