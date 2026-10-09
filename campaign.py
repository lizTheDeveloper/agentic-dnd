"""Campaigns: turning character sheets into agents, and reading state back.

A campaign is one DM agent plus 1-8 player agents.  Each agent is just a
markdown character sheet (its system prompt), a role, and a tool list.
"""

import json
import random
import re
import time
import uuid

import store
from store import key

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no O/0/I/1
CODE_LENGTH = 6

PLAYER_EMOJI = ["⚔️", "🛡️", "🏹", "🔮", "🗡️"]
DM_EMOJI = "🎲"

# Tools available by role (schemas live in tools.py)
DM_TOOLS = [
    "roll_dice", "create_npc", "lookup_rule", "view_npc_sheet",
    "set_scene", "resolve_action", "remember_fact", "recall_facts",
    "take_note", "read_notes",
]
PLAYER_TOOLS = ["roll_dice", "take_note", "read_notes"]


# ---------------------------------------------------------------------------
# Character sheets -> agents
# ---------------------------------------------------------------------------

def parse_agent_markdown(md_text, index=0):
    """Extract agent info from pasted markdown.

    Returns dict: {id, name, role, emoji, system_prompt, tools}
    """
    lines = md_text.strip().split("\n")

    # Extract name from first heading
    name = "Unnamed"
    for line in lines:
        m = re.match(r"^#{1,3}\s+(.+)", line.strip())
        if m:
            name = m.group(1).strip()
            break

    # Detect DM role
    heading_text = name.lower()
    is_dm = any(k in heading_text for k in ["dungeon master", " dm", "dm ", "game master", "gm "])
    # Also check full text for DM role indicators in the first few lines
    if not is_dm:
        preview = "\n".join(lines[:10]).lower()
        is_dm = "dungeon master" in preview or "role: dm" in preview or "role: dungeon master" in preview

    role = "dm" if is_dm else "player"

    # Generate stable id from name
    agent_id = re.sub(r"[^a-z0-9]", "", name.lower())[:20] or f"agent{index}"

    # Emoji assignment
    if role == "dm":
        emoji = DM_EMOJI
    else:
        emoji = PLAYER_EMOJI[index % len(PLAYER_EMOJI)]

    # Tool list by role
    tools = DM_TOOLS if role == "dm" else PLAYER_TOOLS

    return {
        "id": agent_id,
        "name": name,
        "role": role,
        "emoji": emoji,
        "system_prompt": md_text.strip(),
        "tools": tools,
    }


def clean(s, n=200):
    """Trim user input to a string of at most n characters."""
    return str(s or "").strip()[:n]


def build_agents(dm_markdown, character_markdowns, *, dm_name="", dm_emoji="",
                 char_names=(), char_emojis=()):
    """Parse the DM and every character sheet, applying the client's
    name/emoji overrides.  Returns a list with the DM first."""
    dm_agent = parse_agent_markdown(dm_markdown, index=0)
    dm_agent["role"] = "dm"  # force DM role regardless of detection
    dm_agent["emoji"] = dm_emoji or DM_EMOJI
    dm_agent["tools"] = DM_TOOLS
    if dm_name:
        dm_agent["name"] = dm_name

    agents = [dm_agent]
    seen_ids = {dm_agent["id"]}
    for i, md in enumerate(character_markdowns):
        md_text = md.strip() if isinstance(md, str) else ""
        if not md_text:
            continue
        agent = parse_agent_markdown(md_text, index=i + 1)
        agent["role"] = "player"  # force player
        agent["tools"] = PLAYER_TOOLS
        # Apply overrides from client
        if i < len(char_names) and char_names[i]:
            agent["name"] = clean(char_names[i], 80)
        if i < len(char_emojis) and char_emojis[i]:
            agent["emoji"] = clean(char_emojis[i], 10)
        else:
            agent["emoji"] = PLAYER_EMOJI[i % len(PLAYER_EMOJI)]
        # Ensure unique id
        if agent["id"] in seen_ids:
            agent["id"] = f"{agent['id']}_{i}"
        seen_ids.add(agent["id"])
        agents.append(agent)
    return agents


# ---------------------------------------------------------------------------
# Writing a new campaign
# ---------------------------------------------------------------------------

def allocate_code(r):
    """Reserve a fresh room code, or return None after 12 collisions."""
    for _ in range(12):
        cand = "".join(random.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        # Reserve atomically
        if r.hsetnx(key(cand, "meta"), "status", "setup"):
            return cand
    return None


def save_new_campaign(r, code, *, name, creator_id, agents,
                      rulebook_name="", rulebook_chunks=()):
    """Write meta, agents and rulebook chunks for a freshly allocated code."""
    ttl = store.TTL
    now = int(time.time())
    meta_mapping = {
        "name": name,
        "status": "active",
        "current_round": "1",
        "current_agent_index": "0",
        "created_at": str(now),
        "creator_id": str(creator_id),
    }
    if rulebook_name:
        meta_mapping["rulebook_name"] = rulebook_name
    r.hset(key(code, "meta"), mapping=meta_mapping)
    r.expire(key(code, "meta"), ttl)

    for agent in agents:
        r.hset(key(code, "agents"), agent["id"], json.dumps(agent))
    r.expire(key(code, "agents"), ttl)

    # Store rulebook chunks in Redis hash (chunk_id -> JSON).  This is the
    # whole "vector store" -- retrieval.py searches it by keyword.
    if rulebook_chunks:
        for chunk in rulebook_chunks:
            if not isinstance(chunk, dict):
                continue
            chunk_id = chunk.get("id", str(uuid.uuid4())[:8])
            r.hset(key(code, "rulebook"), chunk_id, json.dumps(chunk))
        r.expire(key(code, "rulebook"), ttl)

    # Initialize empty structures with TTL
    r.expire(key(code, "log"), ttl)
    r.expire(key(code, "whispers"), ttl)
    r.expire(key(code, "facts"), ttl)
    r.expire(key(code, "npcs"), ttl)


# ---------------------------------------------------------------------------
# Reading state back
# ---------------------------------------------------------------------------

def load_agents(r, code):
    """Return (agents dict, turn order).  Turn order is DM first, then
    players alphabetically by id."""
    agents_raw = r.hgetall(key(code, "agents")) or {}
    agents = {aid: json.loads(v) for aid, v in agents_raw.items()}
    order = sorted(agents.keys(),
                   key=lambda a: (0 if agents[a].get("role") == "dm" else 1, a))
    return agents, order


def take_whispers_for(r, code, agent_id):
    """Pop pending whispers addressed to agent_id; leave the rest queued."""
    received = []
    remaining = []
    whisper_count = r.llen(key(code, "whispers")) or 0
    for _ in range(whisper_count):
        raw = r.lpop(key(code, "whispers"))
        if raw is None:
            break
        w = json.loads(raw)
        if w.get("to") == agent_id:
            received.append(w)
        else:
            remaining.append(w)
    # Push back non-matching whispers
    for w in remaining:
        r.rpush(key(code, "whispers"), json.dumps(w))
    return received


def load_state(code):
    """Assemble full campaign state for the client (what /state returns)."""
    r = store.redis_client

    meta = r.hgetall(key(code, "meta"))
    if not meta:
        return None

    agents, agent_order = load_agents(r, code)

    # Log entries (most recent last)
    log = [json.loads(x) for x in r.lrange(key(code, "log"), 0, -1) or []]

    # Pending whispers
    whispers = r.lrange(key(code, "whispers"), 0, -1) or []

    # World facts
    facts_raw = r.hgetall(key(code, "facts")) or {}
    facts = {fid: json.loads(v) for fid, v in facts_raw.items()}

    # NPCs
    npcs_raw = r.hgetall(key(code, "npcs")) or {}
    npcs = {nid: json.loads(v) for nid, v in npcs_raw.items()}

    seq = int(r.get(key(code, "_bump")) or 0)

    # Rulebook info (chunk count, not the full content)
    rulebook_count = r.hlen(key(code, "rulebook")) or 0

    return {
        "code": code,
        "name": meta.get("name", ""),
        "status": meta.get("status", "setup"),
        "current_round": int(meta.get("current_round", 0)),
        "current_agent_index": int(meta.get("current_agent_index", 0)),
        "created_at": meta.get("created_at", ""),
        "creator_id": meta.get("creator_id", ""),
        "agents": agents,
        "agent_order": agent_order,
        "log": log,
        "whispers_pending": len(whispers),
        "facts": facts,
        "npcs": npcs,
        "rulebook_name": meta.get("rulebook_name", ""),
        "rulebook_chunks": rulebook_count,
        "seq": seq,
        "now": int(time.time()),
    }
