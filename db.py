"""
db.py — MongoDB persistence layer (async, via Motor).

Collections
-----------
users    one document per Telegram user: profile, status, settings, DeepSeek
         session pointer, usage counters.
tokens   the DeepSeek key pool. Pool size == number of users that can talk to
         DeepSeek at the same time.
history  chat turns. Carries a TTL index so every turn self-destructs 24h
         after it was written, and a nightly job wipes the rest.
config   single 'bot' document: access mode and other global switches.

Everything is per-user scoped: a query for user A can never return user B's
rows because uid is part of every filter.
"""
import logging
import os
import time
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("db")

MONGO_URL = os.getenv("MONGO_URL", "")
DB_NAME = os.getenv("MONGO_DB", "deepseek_bot")

if not MONGO_URL:
    raise SystemExit(
        "❌ Missing MONGO_URL.\n"
        "Set it to your MongoDB connection string, e.g.\n"
        "  MONGO_URL=mongodb+srv://user:pass@cluster.mongodb.net/?appName=Cluster0\n"
        "Never hardcode credentials in source code."
    )

# How long a chat turn survives. The nightly job also clears everything, this
# is the belt-and-braces safety net in case the bot is down at midnight.
HISTORY_TTL_SECONDS = 24 * 3600

_client: Optional[AsyncIOMotorClient] = None
_db = None

# Status values
ACTIVE = "active"
BLOCKED = "blocked"
PENDING = "pending"

# Access modes
MODE_INVITE = "invite"   # only approved users
MODE_OPEN = "open"       # anyone who presses /start


async def connect() -> None:
    """Open the pool and make sure indexes exist. Safe to call once at boot."""
    global _client, _db
    _client = AsyncIOMotorClient(
        MONGO_URL,
        serverSelectionTimeoutMS=15000,
        connectTimeoutMS=15000,
        retryWrites=True,
    )
    _db = _client[DB_NAME]
    await _client.admin.command("ping")

    await _db.users.create_index([("status", ASCENDING)])
    await _db.users.create_index([("last_seen", DESCENDING)])
    await _db.tokens.create_index([("label", ASCENDING)], unique=True)
    await _db.history.create_index([("uid", ASCENDING), ("ts", ASCENDING)])
    # TTL: Mongo deletes the document once expires_at is in the past
    await _db.history.create_index("expires_at", expireAfterSeconds=0)
    await _db.invites.create_index([("created_at", DESCENDING)])

    log.info("MongoDB connected (db=%s)", DB_NAME)


async def close() -> None:
    if _client:
        _client.close()


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "_id": "bot",
    "access_mode": MODE_INVITE,
    "welcome_note": "",
}


async def get_config() -> Dict[str, Any]:
    doc = await _db.config.find_one({"_id": "bot"})
    if not doc:
        await _db.config.insert_one(dict(DEFAULT_CONFIG))
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(doc)
    return merged


async def set_config(**kv) -> None:
    await _db.config.update_one({"_id": "bot"}, {"$set": kv}, upsert=True)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "model_type": "default",
    "thinking": False,
    "search": False,
    "voice_reply": False,
    "tts_female": True,
    "auto_urls": True,
    "persona": "default",
}


async def get_user(uid: int) -> Optional[Dict[str, Any]]:
    return await _db.users.find_one({"_id": uid})


async def upsert_user(uid: int, *, username: str = "", first_name: str = "",
                      status: Optional[str] = None,
                      role: Optional[str] = None) -> Dict[str, Any]:
    """Create the user on first contact, otherwise just refresh last_seen."""
    on_insert: Dict[str, Any] = {
        "joined_at": _now(),
        "settings": dict(DEFAULT_SETTINGS),
        "session_id": None,
        "parent_msg_id": None,
        "attached_files": [],
        "msg_count": 0,
        "chars_in": 0,
        "chars_out": 0,
        "status": status or PENDING,
        "role": role or "user",
    }
    set_fields: Dict[str, Any] = {"last_seen": _now()}
    if username:
        set_fields["username"] = username
    if first_name:
        set_fields["first_name"] = first_name
    if status:
        set_fields["status"] = status
        on_insert.pop("status", None)
    if role:
        set_fields["role"] = role
        on_insert.pop("role", None)

    return await _db.users.find_one_and_update(
        {"_id": uid},
        {"$set": set_fields, "$setOnInsert": on_insert},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def set_user_status(uid: int, status: str) -> bool:
    r = await _db.users.update_one({"_id": uid}, {"$set": {"status": status}})
    return r.matched_count > 0


async def set_user_field(uid: int, **kv) -> None:
    await _db.users.update_one({"_id": uid}, {"$set": kv})


async def bump_usage(uid: int, *, messages: int = 0,
                     chars_in: int = 0, chars_out: int = 0) -> None:
    await _db.users.update_one(
        {"_id": uid},
        {"$inc": {"msg_count": messages,
                  "chars_in": chars_in,
                  "chars_out": chars_out}},
    )


async def list_users(status: Optional[str] = None, skip: int = 0,
                     limit: int = 8) -> List[Dict[str, Any]]:
    q = {"status": status} if status else {}
    cur = _db.users.find(q).sort("last_seen", DESCENDING).skip(skip).limit(limit)
    return [d async for d in cur]


async def count_users(status: Optional[str] = None) -> int:
    q = {"status": status} if status else {}
    return await _db.users.count_documents(q)


async def all_active_ids() -> List[int]:
    cur = _db.users.find({"status": ACTIVE}, {"_id": 1})
    return [d["_id"] async for d in cur]


async def global_stats() -> Dict[str, Any]:
    agg = _db.users.aggregate([{"$group": {
        "_id": None,
        "msgs": {"$sum": "$msg_count"},
        "cin": {"$sum": "$chars_in"},
        "cout": {"$sum": "$chars_out"},
    }}])
    totals = {"msgs": 0, "cin": 0, "cout": 0}
    async for d in agg:
        totals = {"msgs": d.get("msgs", 0),
                  "cin": d.get("cin", 0),
                  "cout": d.get("cout", 0)}
    day_ago = _now() - 86400
    return {
        "total": await count_users(),
        "active": await count_users(ACTIVE),
        "blocked": await count_users(BLOCKED),
        "pending": await count_users(PENDING),
        "active_24h": await _db.users.count_documents({"last_seen": {"$gte": day_ago}}),
        "messages": totals["msgs"],
        "chars_in": totals["cin"],
        "chars_out": totals["cout"],
        "turns_stored": await _db.history.count_documents({}),
    }


# --------------------------------------------------------------------------
# Instance lock
# --------------------------------------------------------------------------

# Only one process may poll Telegram at a time. Two pollers cause
# "Conflict: terminated by other getUpdates request" and updates get split
# randomly between them. This lease lets a new deploy take over cleanly while
# blocking accidental duplicates (e.g. a local run against the live token).
LOCK_TTL = 45          # a holder must refresh within this many seconds
LOCK_REFRESH = 20      # how often the holder refreshes


async def acquire_instance_lock(instance_id: str, force: bool = False) -> bool:
    """Claim the polling lease. True if we own it."""
    now = _now()
    cutoff = now - LOCK_TTL
    q = {"_id": "poller"}
    if not force:
        # take it only if nobody holds it, it's stale, or it's already ours
        q = {"_id": "poller", "$or": [
            {"heartbeat": {"$lt": cutoff}},
            {"owner": instance_id},
        ]}
    try:
        res = await _db.locks.find_one_and_update(
            q,
            {"$set": {"owner": instance_id, "heartbeat": now,
                      "since": now}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return bool(res) and res.get("owner") == instance_id
    except DuplicateKeyError:
        # someone else inserted first — check whether their lease is stale
        cur = await _db.locks.find_one({"_id": "poller"})
        if cur and cur.get("heartbeat", 0) < cutoff:
            return await acquire_instance_lock(instance_id, force=True)
        return False


async def refresh_instance_lock(instance_id: str) -> bool:
    """Heartbeat. False means we lost the lease and must stop polling."""
    res = await _db.locks.update_one(
        {"_id": "poller", "owner": instance_id},
        {"$set": {"heartbeat": _now()}})
    return res.matched_count > 0


async def release_instance_lock(instance_id: str) -> None:
    await _db.locks.delete_one({"_id": "poller", "owner": instance_id})


async def instance_lock_holder() -> Optional[Dict[str, Any]]:
    return await _db.locks.find_one({"_id": "poller"})


# --------------------------------------------------------------------------
# Invites
# --------------------------------------------------------------------------

async def create_invite(code: str, created_by: int, *, max_uses: int = 1,
                        expires_in_h: int = 0, note: str = "") -> Dict[str, Any]:
    """Store a fresh invite code. expires_in_h=0 means it never expires."""
    doc = {
        "_id": code,
        "created_by": created_by,
        "created_at": _now(),
        "max_uses": max_uses,
        "uses": 0,
        "used_by": [],
        "revoked": False,
        "note": note,
        "expires_at": (_now() + expires_in_h * 3600) if expires_in_h else 0,
    }
    await _db.invites.insert_one(doc)
    return doc


async def get_invite(code: str) -> Optional[Dict[str, Any]]:
    return await _db.invites.find_one({"_id": code})


async def list_invites(include_dead: bool = False) -> List[Dict[str, Any]]:
    q = {} if include_dead else {"revoked": False}
    cur = _db.invites.find(q).sort("created_at", DESCENDING).limit(25)
    return [d async for d in cur]


async def revoke_invite(code: str) -> bool:
    r = await _db.invites.update_one({"_id": code},
                                     {"$set": {"revoked": True}})
    return r.matched_count > 0


def invite_state(inv: Dict[str, Any]) -> str:
    """'ok' | 'revoked' | 'expired' | 'used_up'"""
    if not inv or inv.get("revoked"):
        return "revoked"
    exp = inv.get("expires_at") or 0
    if exp and _now() > exp:
        return "expired"
    if inv.get("uses", 0) >= inv.get("max_uses", 1):
        return "used_up"
    return "ok"


async def redeem_invite(code: str, uid: int) -> str:
    """
    Atomically consume one use of `code` for `uid`.

    Returns 'ok' on success, otherwise the reason it failed. The filter does
    the checking, so two people racing on the last slot cannot both win.
    """
    inv = await get_invite(code)
    if not inv:
        return "invalid"
    # A user re-opening their own link must not be rejected, even once the
    # link is otherwise used up or expired.
    if uid in (inv.get("used_by") or []):
        return "revoked" if inv.get("revoked") else "ok"
    state = invite_state(inv)
    if state != "ok":
        return state

    res = await _db.invites.find_one_and_update(
        {"_id": code, "revoked": False,
         "$expr": {"$lt": ["$uses", "$max_uses"]}},
        {"$inc": {"uses": 1}, "$addToSet": {"used_by": uid}},
        return_document=ReturnDocument.AFTER,
    )
    if not res:
        return "used_up"
    return "ok"


# --------------------------------------------------------------------------
# DeepSeek token pool
# --------------------------------------------------------------------------

async def add_token(token: str, label: str) -> bool:
    try:
        await _db.tokens.insert_one({
            "token": token,
            "label": label,
            "added_at": _now(),
            "healthy": True,
            "uses": 0,
            "last_error": "",
            "auth_type": "token",
            "email": "",
            "password": "",
            "last_login": 0,
            "last_check": 0,
        })
        return True
    except DuplicateKeyError:
        return False


async def add_email_account(label: str, email: str, password: str, token: str) -> bool:
    """Add account with email/password + token. Duplicate label -> False."""
    try:
        await _db.tokens.insert_one({
            "token": token,
            "label": label,
            "added_at": _now(),
            "healthy": True,
            "uses": 0,
            "last_error": "",
            "auth_type": "email",
            "email": email,
            "password": password,
            "last_login": _now(),
            "last_check": _now(),
        })
        return True
    except DuplicateKeyError:
        return False


async def update_account_token(label: str, new_token: str) -> bool:
    r = await _db.tokens.update_one(
        {"label": label},
        {"$set": {"token": new_token, "healthy": True, "last_error": "", "last_login": _now(), "last_check": _now()}}
    )
    return r.matched_count > 0


async def set_account_credentials(label: str, email: str, password: str) -> bool:
    r = await _db.tokens.update_one(
        {"label": label},
        {"$set": {"email": email, "password": password, "auth_type": "email"}}
    )
    return r.matched_count > 0


async def get_token_doc(label: str) -> Optional[Dict[str, Any]]:
    return await _db.tokens.find_one({"label": label})


async def remove_token(label: str) -> bool:
    r = await _db.tokens.delete_one({"label": label})
    return r.deleted_count > 0


async def list_tokens() -> List[Dict[str, Any]]:
    cur = _db.tokens.find({}).sort("added_at", ASCENDING)
    return [d async for d in cur]


async def mark_token(label: str, *, healthy: bool, error: str = "") -> None:
    await _db.tokens.update_one(
        {"label": label},
        {"$set": {"healthy": healthy, "last_error": error[:300], "last_check": _now()}})


async def bump_token_use(label: str) -> None:
    await _db.tokens.update_one({"label": label}, {"$inc": {"uses": 1}})


async def set_token_last_check(label: str, healthy: bool = True) -> None:
    await _db.tokens.update_one({"label": label}, {"$set": {"last_check": _now(), "healthy": healthy}})


# --------------------------------------------------------------------------
# History  (per-user, auto-expiring)
# --------------------------------------------------------------------------

async def add_turn(uid: int, role: str, text: str) -> None:
    now = _now()
    await _db.history.insert_one({
        "uid": uid,
        "role": role,
        "text": text[:20000],
        "ts": now,
        "expires_at": _mongo_dt(now + HISTORY_TTL_SECONDS),
    })


def _mongo_dt(epoch: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


async def get_history(uid: int, limit: int = 100) -> List[Dict[str, Any]]:
    """Only ever returns rows belonging to `uid` — cross-user reads impossible."""
    cur = _db.history.find({"uid": uid}).sort("ts", DESCENDING).limit(limit)
    rows = [d async for d in cur]
    return list(reversed(rows))


async def clear_history(uid: int) -> int:
    r = await _db.history.delete_many({"uid": uid})
    return r.deleted_count


async def wipe_all_history() -> int:
    """Nightly reset: drop every stored turn and detach every DeepSeek session."""
    r = await _db.history.delete_many({})
    await clear_all_sessions()
    return r.deleted_count


async def clear_all_sessions() -> int:
    """
    Detach EVERY user's cached DeepSeek session pointer.

    Needed after a pool-wide chat wipe: delete_all on all accounts kills every
    cloud session at once, so all cached session_id/parent_msg_id pairs become
    stale. Resetting them up-front means users get a clean session on their
    next message instead of relying on stale-session auto-recovery.
    """
    r = await _db.users.update_many(
        {},
        {"$set": {"session_id": None, "session_key": None,
                  "parent_msg_id": None, "attached_files": []}})
    return r.modified_count
