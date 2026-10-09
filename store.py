"""Redis: the one place all campaign state lives.

Every campaign gets a 6-character room code, and everything about it is
stored under dnd:{code}:*.  TTL is refreshed to 24h on activity, so an
abandoned campaign quietly disappears a day later.

Keys:
  dnd:{code}:meta          HASH  campaign name, status, round tracking
  dnd:{code}:agents        HASH  agent_id -> JSON config
  dnd:{code}:log           LIST  ordered turn entries (JSON)
  dnd:{code}:whispers      LIST  pending whispers (JSON)
  dnd:{code}:facts         HASH  fact_id -> JSON world facts      (shared memory)
  dnd:{code}:notes:{aid}   HASH  per-agent private notes          (private memory)
  dnd:{code}:npcs          HASH  npc_id -> JSON npc data
  dnd:{code}:rulebook      HASH  chunk_id -> JSON rulebook chunk  (the RAG corpus)
  dnd:{code}:_bump         INT   change-detection counter for polls

Outside the campaign namespace:
  dnd:spend:{code}         FLOAT estimated inference spend in cents
  dnd:rl:{bucket}:{ident}  INT   rate-limit counters
"""

import os

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# decode_responses=True so every read comes back as str, not bytes.
# Creating the client does not connect; the first command does.
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

TTL = 24 * 3600  # campaigns expire after 24 hours of inactivity


def key(code, suffix=""):
    """dnd:{code} or dnd:{code}:{suffix}."""
    return f"dnd:{code}" + (f":{suffix}" if suffix else "")


def spend_key(code):
    return f"dnd:spend:{code}"


def all_keys(code, agent_ids=None):
    """Return every Redis key for a campaign so TTL can be refreshed."""
    keys = [
        key(code, "meta"), key(code, "agents"), key(code, "log"),
        key(code, "whispers"), key(code, "facts"), key(code, "npcs"),
        key(code, "rulebook"), key(code, "_bump"),
    ]
    for aid in (agent_ids or []):
        keys.append(key(code, f"notes:{aid}"))
    return keys


def touch(code, agent_ids=None):
    for k in all_keys(code, agent_ids):
        redis_client.expire(k, TTL)


def bump(code):
    """Increment the change counter so polling clients know to re-render."""
    seq = redis_client.incr(key(code, "_bump"))
    touch(code, get_agent_ids(code))
    return seq


def get_agent_ids(code):
    """Return list of agent ids from the agents hash."""
    raw = redis_client.hkeys(key(code, "agents"))
    return [k for k in raw] if raw else []


def current_round(code, r):
    """Read current round number from campaign meta."""
    try:
        val = r.hget(key(code, "meta"), "current_round")
        return int(val) if val else 1
    except Exception:
        return 1
