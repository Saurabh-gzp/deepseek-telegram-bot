"""
token_pool.py — DeepSeek key pool with hard concurrency control + email/pass auto-refresh.

The rule the pool enforces: one DeepSeek account/key can serve exactly one request at
a time. So N accounts == N users talking to DeepSeek simultaneously; with 1 account
the bot answers one person at a time and everyone else waits in line with an animated
"Analysing your request" loader.

New in this version:
  • Each account can be stored as email+password. Token is auto-refreshed when it expires.
  • Health checks + auto-refresh on failure.
  • Waiting queue tracking for UI (queue position).
  • Admin notifications on account failure.
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import db
from deepseek_client import (DeepSeekClient, login_with_credentials,
                             login_with_credentials_ex, validate_token)

log = logging.getLogger("pool")


class Lease:
    """A single checked-out key plus its ready-to-use client."""

    def __init__(self, label: str, token: str, client: DeepSeekClient, email: str = ""):
        self.label = label
        self.token = token
        self.client = client
        self.email = email
        self.acquired_at = time.monotonic()


class TokenPool:
    def __init__(self, workdir: str = "."):
        self.workdir = workdir
        self._clients: Dict[str, DeepSeekClient] = {}   # label -> client
        self._free: Optional[asyncio.Queue] = None      # queue of labels
        self._labels: List[str] = []
        self._tokens: Dict[str, str] = {}               # label -> token
        self._emails: Dict[str, str] = {}               # label -> email
        self._passwords: Dict[str, str] = {}            # label -> password
        self._auth_types: Dict[str, str] = {}          # label -> auth_type
        self._busy: Dict[str, int] = {}                 # label -> uid
        self._pref: Dict[int, str] = {}                 # uid -> last-used label (session affinity)
        self._waiters: int = 0
        self._lock = asyncio.Lock()

    # ---------- lifecycle ----------

    async def reload(self) -> int:
        """(Re)build the pool from the tokens collection. Returns pool size."""
        async with self._lock:
            rows = await db.list_tokens()
            healthy = [r for r in rows if r.get("healthy", True)]

            self._labels = [r["label"] for r in healthy]
            self._tokens = {r["label"]: r["token"] for r in healthy}
            self._emails = {r["label"]: r.get("email", "") for r in healthy}
            self._passwords = {r["label"]: r.get("password", "") for r in healthy}
            self._auth_types = {r["label"]: r.get("auth_type", "token") for r in healthy}

            # keep existing clients so we don't re-download the WASM each time
            for label in list(self._clients):
                if label not in self._tokens:
                    self._clients.pop(label, None)
            for label, token in self._tokens.items():
                existing = self._clients.get(label)
                if existing is None or existing.token != token:
                    self._clients[label] = DeepSeekClient(token, workdir=self.workdir)

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

    @property
    def waiting_count(self) -> int:
        return self._waiters

    def busy_map(self) -> Dict[str, int]:
        return dict(self._busy)

    def get_account_info(self, label: str) -> Dict[str, str]:
        return {
            "label": label,
            "token": self._tokens.get(label, ""),
            "email": self._emails.get(label, ""),
            "auth_type": self._auth_types.get(label, "token"),
        }

    # ---------- refresh logic ----------

    async def refresh_account(self, label: str) -> Tuple[bool, str]:
        """
        Try to refresh token for account with email/password.
        Returns (success, message).
        """
        doc = await db.get_token_doc(label)
        if not doc:
            return False, "Account not found"
        email = doc.get("email", "")
        password = doc.get("password", "")
        auth_type = doc.get("auth_type", "token")
        if auth_type != "email" or not email or not password:
            return False, "No email/password stored — add email/pass to enable auto-refresh"
        # Try login
        try:
            new_token, reason = await asyncio.to_thread(
                login_with_credentials_ex, email, password)
        except Exception as e:
            return False, f"Login exception: {e}"
        if not new_token:
            await db.mark_token(label, healthy=False,
                                error=f"Login failed ({reason})")
            if reason == "bot_blocked":
                return False, ("DeepSeek anti-bot ne server ka login block kiya "
                               "(cloud IP). Manual token add karo ya DS_PROXY set karo.")
            return False, f"Login failed — {reason}"
        ok = await db.update_account_token(label, new_token)
        if ok:
            # update in-memory
            self._tokens[label] = new_token
            # recreate client
            self._clients[label] = DeepSeekClient(new_token, workdir=self.workdir)
            await db.set_token_last_check(label, healthy=True)
            log.info("Refreshed token for %s (%s)", label, email)
            return True, "Token refreshed successfully"
        return False, "DB update failed"

    async def health_check_all(self, notify_callback=None) -> Dict[str, any]:
        """
        Check all accounts health. For email accounts, try refresh if token invalid.
        notify_callback(label, email, error) will be called on failure if provided.
        """
        rows = await db.list_tokens()
        results = {"total": len(rows), "healthy": 0, "refreshed": 0, "failed": 0, "errors": []}
        for r in rows:
            label = r["label"]
            token = r["token"]
            email = r.get("email", "")
            auth_type = r.get("auth_type", "token")
            healthy = r.get("healthy", True)
            # Skip already known unhealthy? Still try to refresh email accounts
            is_valid = await asyncio.to_thread(validate_token, token, self.workdir)
            if is_valid:
                if not healthy:
                    await db.mark_token(label, healthy=True, error="")
                await db.set_token_last_check(label, healthy=True)
                results["healthy"] += 1
            else:
                # Token invalid
                if auth_type == "email" and email:
                    # try refresh
                    success, msg = await self.refresh_account(label)
                    if success:
                        results["refreshed"] += 1
                        results["healthy"] += 1
                    else:
                        results["failed"] += 1
                        results["errors"].append(f"{label} ({email}): {msg}")
                        if notify_callback:
                            try:
                                await notify_callback(label, email, msg)
                            except: pass
                else:
                    await db.mark_token(label, healthy=False, error="Token expired — no email/pass for auto-refresh")
                    results["failed"] += 1
                    results["errors"].append(f"{label}: Token expired — no auto-refresh")
                    if notify_callback:
                        try:
                            await notify_callback(label, email or "token", "Token expired — no email/pass")
                        except: pass
        # reload pool to reflect health changes
        await self.reload()
        return results

    async def report_failure(self, label: str, error: str, notify_callback=None) -> bool:
        """
        Called when a DeepSeek request fails on a specific account.
        Detects auth errors and tries auto-refresh. Returns True if refreshed.
        """
        low = error.lower()
        # NOTE: "session" must NOT be here — DeepSeek session errors
        # ("invalid chat session id") are account-scoped state problems,
        # not auth problems. Treating them as auth errors marked healthy
        # accounts unhealthy and burned email-login refreshes (v7.6 fix).
        is_auth = any(x in low for x in ["401", "403", "unauthorized", "authentication", "token", "expired", "login"])
        if not is_auth:
            await db.mark_token(label, healthy=True, error=error[:200])
            return False
        # Try refresh if email account
        doc = await db.get_token_doc(label)
        if not doc:
            return False
        if doc.get("auth_type") == "email" and doc.get("email"):
            success, msg = await self.refresh_account(label)
            if success:
                log.info("Auto-refreshed %s after auth error", label)
                return True
            else:
                await db.mark_token(label, healthy=False, error=f"Auth failed: {error[:150]} | Refresh: {msg}")
                if notify_callback:
                    try:
                        await notify_callback(label, doc.get("email", ""), f"Auth error: {error[:150]} | Refresh failed: {msg}")
                    except: pass
                return False
        else:
            await db.mark_token(label, healthy=False, error=f"Auth error: {error[:150]} — no auto-refresh")
            if notify_callback:
                try:
                    await notify_callback(label, "token", f"Auth error: {error[:150]}")
                except: pass
            return False

    # ---------- leasing with queue tracking ----------

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
        self._waiters += 1
        try:
            if timeout is None:
                label = await self._free.get()
            else:
                label = await asyncio.wait_for(self._free.get(), timeout)
        except asyncio.TimeoutError:
            yield None
            return
        finally:
            self._waiters = max(0, self._waiters - 1)

        # Session affinity: a DeepSeek chat session is only valid on the
        # account that created it. Handing a user a different key than last
        # time would break their cached session ("invalid chat session id").
        # So when this user's previous key is free right now, swap it in
        # (never preempting a busy key — FIFO order keeps fairness).
        pref = self._pref.get(uid)
        if pref and pref != label and pref in self._tokens and not self._free.empty():
            drained = []
            found = False
            while not self._free.empty():
                try:
                    lab = self._free.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if lab == pref:
                    found = True
                    break
                drained.append(lab)
            if found:
                self._free.put_nowait(label)   # give the FIFO pick back
                label = pref
            for lab in drained:
                self._free.put_nowait(lab)

        self._pref[uid] = label
        if len(self._pref) > 1000:  # bounded memory
            self._pref.pop(next(iter(self._pref)), None)

        self._busy[label] = uid
        try:
            await db.bump_token_use(label)
            yield Lease(label, self._tokens[label], self._clients[label], self._emails.get(label, ""))
        finally:
            self._busy.pop(label, None)
            # only return it if the key still exists after a concurrent reload
            if label in self._tokens and self._free is not None:
                self._free.put_nowait(label)

    async def wipe_all_accounts(self) -> Dict[str, Any]:
        """
        App equivalent of Settings → Data controls → Delete all chats — but
        across EVERY pooled account, not just the caller's current key.

        The bot spreads users over the whole pool, so a user's chats live on
        several accounts at once; wiping one key leaves the rest dirty. This
        iterates ALL accounts in the tokens collection (healthy or not — a
        flagged-but-valid token can still delete its chats) and fires
        delete_all on each account's own token concurrently. Future accounts
        are covered automatically because the list comes from the DB.

        Never leases keys (a busy key would block/queue the wipe) — the
        session auto-recovery added in v7.4 heals any stream that was
        mid-flight on a wiped account.

        Returns {"total": n, "wiped": [labels], "failed": [(label, reason)]}.
        """
        rows = await db.list_tokens()
        if not rows:
            return {"total": 0, "wiped": [], "failed": []}

        async def _one(row) -> Tuple[str, bool, str]:
            label = row["label"]
            client = self._clients.get(label)
            if client is None or client.token != row.get("token"):
                client = DeepSeekClient(row["token"], workdir=self.workdir)
            try:
                ok, detail = await asyncio.to_thread(client.delete_all_chats)
                return label, ok, detail
            except Exception as e:
                return label, False, str(e)

        results = await asyncio.gather(*[_one(r) for r in rows])
        wiped = [lab for lab, ok, _ in results if ok]
        failed = [(lab, d) for lab, ok, d in results if not ok]
        log.info("Pool-wide chat wipe: %d/%d accounts wiped%s",
                 len(wiped), len(rows),
                 "" if not failed else f" (failed: {[l for l, _ in failed]})")
        return {"total": len(rows), "wiped": wiped, "failed": failed}

    async def would_wait(self) -> bool:
        """True if every key is currently in use (so the caller will queue)."""
        return self.size > 0 and self.free_count == 0

    async def queue_position_estimate(self) -> int:
        """Estimate of queued users + 1 (for display)."""
        return self._waiters + self.busy_count


POOL = TokenPool()
