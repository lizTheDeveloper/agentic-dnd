"""Agentic D&D — multi-agent campaign tool.

Students drop in markdown character sheets (1 DM + 3-5 players) and watch
AI agents interact in a turn-based campaign with tool use, whispers, and
chain-of-thought.  State lives in Redis (same room-code pattern as Data Crimes).

Keys (all under dnd:{code}:*, TTL refreshed to 24h on activity):
  dnd:{code}:meta          HASH  campaign name, status, round tracking
  dnd:{code}:agents        HASH  agent_id -> JSON config
  dnd:{code}:log           LIST  ordered turn entries (JSON)
  dnd:{code}:whispers      LIST  pending whispers (JSON)
  dnd:{code}:facts         HASH  fact_id -> JSON world facts
  dnd:{code}:notes:{aid}   HASH  per-agent private notes
  dnd:{code}:npcs          HASH  npc_id -> JSON npc data
  dnd:{code}:_bump         INT   change-detection counter for polls
"""

import json
import os
import random
import re
import time
import uuid

from flask import Blueprint, Response, jsonify, request, session

agentic_dnd_bp = Blueprint("agentic_dnd", __name__)

TTL = 24 * 3600  # campaigns expire after 24 hours of inactivity
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no O/0/I/1
CODE_LENGTH = 6
MAX_MARKDOWN_BYTES = 64_000  # per agent system prompt (generous — models have large context)
MAX_LOG_ENTRIES = 0  # 0 = unlimited — sessions can run as long as the student wants
MAX_ROUNDS = 0  # 0 = unlimited — context compaction handles long sessions gracefully
APP_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "static", "tools", "agentic-dnd.html")

PLAYER_EMOJI = ["⚔️", "🛡️", "🏹", "🔮", "🗡️"]
DM_EMOJI = "🎲"

# Tools available by role
DM_TOOLS = [
    "roll_dice", "create_npc", "lookup_rule", "view_npc_sheet",
    "set_scene", "resolve_action", "remember_fact", "recall_facts",
    "take_note", "read_notes",
]
PLAYER_TOOLS = ["roll_dice", "take_note", "read_notes"]


# ---------------------------------------------------------------------------
# Redis helpers (mirror data_crimes.py patterns)
# ---------------------------------------------------------------------------

def _r():
    from server import redis_client
    return redis_client


def _k(code, suffix=""):
    return f"dnd:{code}" + (f":{suffix}" if suffix else "")


def _all_keys(code, agent_ids=None):
    """Return every Redis key for a campaign so TTL can be refreshed."""
    keys = [
        _k(code, "meta"), _k(code, "agents"), _k(code, "log"),
        _k(code, "whispers"), _k(code, "facts"), _k(code, "npcs"),
        _k(code, "rulebook"), _k(code, "_bump"),
    ]
    for aid in (agent_ids or []):
        keys.append(_k(code, f"notes:{aid}"))
    return keys


def _touch(code, agent_ids=None):
    r = _r()
    for k in _all_keys(code, agent_ids):
        r.expire(k, TTL)


def _bump(code):
    r = _r()
    seq = r.incr(_k(code, "_bump"))
    _touch(code, _get_agent_ids(code))
    return seq


def _get_agent_ids(code):
    """Return list of agent ids from the agents hash."""
    r = _r()
    raw = r.hkeys(_k(code, "agents"))
    return [k for k in raw] if raw else []


def _rate_limited(bucket, ident, limit, window):
    try:
        r = _r()
        key = f"dnd:rl:{bucket}:{ident}"
        c = r.incr(key)
        if c == 1:
            r.expire(key, window)
        return c > limit
    except Exception:
        return False


def _rate_ident():
    """Authenticated user ID for rate limiting (not spoofable like X-Forwarded-For)."""
    uid = session.get("user_id")
    return f"u:{uid}" if uid else f"ip:{request.remote_addr or '?'}"


def _clean(s, n=200):
    return str(s or "").strip()[:n]


# ---------------------------------------------------------------------------
# Auth helper — require_auth from the app
# ---------------------------------------------------------------------------

def _require_auth(f):
    """Lightweight auth check: ensures session has a user_id.
    In FLASK_DEBUG mode, auto-sets a dev user so the tool works without a database.
    """
    from functools import wraps
    from flask import current_app

    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if current_app.debug:
                session["user_id"] = "dev-user-1"
                session["email"] = "dev@localhost"
            else:
                return jsonify({"error": "Login required"}), 401
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------

def _parse_agent_markdown(md_text, index=0):
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


# ---------------------------------------------------------------------------
# State assembly (for polls)
# ---------------------------------------------------------------------------

def _state(code):
    """Assemble full campaign state for the client."""
    r = _r()

    meta_raw = r.hgetall(_k(code, "meta"))
    if not meta_raw:
        return None

    meta = {k: v for k, v in meta_raw.items()}

    # Parse agents
    agents_raw = r.hgetall(_k(code, "agents")) or {}
    agents = {}
    agent_order = []
    for aid, v in agents_raw.items():
        agents[aid] = json.loads(v)
        agent_order.append(aid)
    # Sort: DM first, then by original index
    agent_order.sort(key=lambda a: (0 if agents[a].get("role") == "dm" else 1, a))

    # Log entries (most recent last)
    log_raw = r.lrange(_k(code, "log"), 0, -1) or []
    log = [json.loads(x) for x in log_raw]

    # Pending whispers
    whispers_raw = r.lrange(_k(code, "whispers"), 0, -1) or []
    whispers = [json.loads(x) for x in whispers_raw]

    # World facts
    facts_raw = r.hgetall(_k(code, "facts")) or {}
    facts = {fid: json.loads(v) for fid, v in facts_raw.items()}

    # NPCs
    npcs_raw = r.hgetall(_k(code, "npcs")) or {}
    npcs = {nid: json.loads(v) for nid, v in npcs_raw.items()}

    seq = int(r.get(_k(code, "_bump")) or 0)

    # Rulebook info (chunk count, not the full content)
    rulebook_count = r.hlen(_k(code, "rulebook")) or 0

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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# ---- Serve the client HTML ------------------------------------------------
@agentic_dnd_bp.route("/tools/agentic-dnd")
def serve_app():
    try:
        with open(APP_HTML, encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")
    except FileNotFoundError:
        return Response("Agentic D&D app not built", status=500)


# ---- Create campaign ------------------------------------------------------
@agentic_dnd_bp.route("/tools/agentic-dnd/create", methods=["POST"])
@_require_auth
def create_campaign():
    if _rate_limited("create", _rate_ident(), 10, 3600):
        return jsonify({"error": "Too many campaigns started. Take a breather."}), 429

    data = request.get_json(silent=True) or {}
    campaign_name = _clean(data.get("name"), 100) or "Untitled Campaign"
    dm_markdown = (data.get("dm_markdown") or "").strip()
    character_markdowns = data.get("characters") or []

    # Name / emoji overrides sent by the client
    dm_name_override = _clean(data.get("dm_name"), 80) or ""
    dm_emoji_override = _clean(data.get("dm_emoji"), 10) or ""
    char_names = data.get("char_names") or []   # list of strings
    char_emojis = data.get("char_emojis") or []  # list of strings

    # Rulebook chunks uploaded by the student
    rulebook_chunks = data.get("rulebook_chunks") or []
    rulebook_name = _clean(data.get("rulebook_name"), 100) or ""

    if not dm_markdown:
        return jsonify({"error": "A Dungeon Master markdown is required."}), 400

    if len(dm_markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        return jsonify({"error": f"DM markdown exceeds {MAX_MARKDOWN_BYTES // 1000}KB limit."}), 400

    if not isinstance(character_markdowns, list) or len(character_markdowns) < 1:
        return jsonify({"error": "At least one character markdown is required."}), 400

    if len(character_markdowns) > 8:
        return jsonify({"error": "Maximum 8 characters per campaign."}), 400

    for i, md in enumerate(character_markdowns):
        if isinstance(md, str) and len(md.encode("utf-8")) > MAX_MARKDOWN_BYTES:
            return jsonify({"error": f"Character {i+1} markdown exceeds {MAX_MARKDOWN_BYTES // 1000}KB limit."}), 400

    # Cap rulebook at 200 chunks / 256KB total
    if len(rulebook_chunks) > 200:
        return jsonify({"error": "Rulebook exceeds 200 chunk limit."}), 400
    rb_total = sum(len(json.dumps(c)) for c in rulebook_chunks if isinstance(c, dict))
    if rb_total > 256_000:
        return jsonify({"error": "Rulebook chunks exceed 256KB total."}), 400

    r = _r()

    # Allocate room code
    code = None
    for _ in range(12):
        cand = "".join(random.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        # Reserve atomically
        if r.hsetnx(_k(cand, "meta"), "status", "setup"):
            code = cand
            break
    if not code:
        return jsonify({"error": "Could not allocate a campaign code. Try again."}), 503

    # Parse DM
    dm_agent = _parse_agent_markdown(dm_markdown, index=0)
    dm_agent["role"] = "dm"  # force DM role regardless of detection
    dm_agent["emoji"] = dm_emoji_override or DM_EMOJI
    dm_agent["tools"] = DM_TOOLS
    if dm_name_override:
        dm_agent["name"] = dm_name_override

    # Parse characters
    agents = [dm_agent]
    seen_ids = {dm_agent["id"]}
    for i, md in enumerate(character_markdowns):
        md_text = md.strip() if isinstance(md, str) else ""
        if not md_text:
            continue
        agent = _parse_agent_markdown(md_text, index=i + 1)
        agent["role"] = "player"  # force player
        agent["tools"] = PLAYER_TOOLS
        # Apply overrides from client
        if i < len(char_names) and char_names[i]:
            agent["name"] = _clean(char_names[i], 80)
        if i < len(char_emojis) and char_emojis[i]:
            agent["emoji"] = _clean(char_emojis[i], 10)
        else:
            agent["emoji"] = PLAYER_EMOJI[i % len(PLAYER_EMOJI)]
        # Ensure unique id
        if agent["id"] in seen_ids:
            agent["id"] = f"{agent['id']}_{i}"
        seen_ids.add(agent["id"])
        agents.append(agent)

    # Write meta hash
    now = int(time.time())
    meta_mapping = {
        "name": campaign_name,
        "status": "active",
        "current_round": "1",
        "current_agent_index": "0",
        "created_at": str(now),
        "creator_id": str(session.get("user_id", "")),
    }
    if rulebook_name:
        meta_mapping["rulebook_name"] = rulebook_name
    r.hset(_k(code, "meta"), mapping=meta_mapping)
    r.expire(_k(code, "meta"), TTL)

    # Write agents hash
    for agent in agents:
        r.hset(_k(code, "agents"), agent["id"], json.dumps(agent))
    r.expire(_k(code, "agents"), TTL)

    # Store rulebook chunks in Redis hash (chunk_id -> JSON)
    if rulebook_chunks:
        for chunk in rulebook_chunks:
            if not isinstance(chunk, dict):
                continue
            chunk_id = chunk.get("id", str(uuid.uuid4())[:8])
            r.hset(_k(code, "rulebook"), chunk_id, json.dumps(chunk))
        r.expire(_k(code, "rulebook"), TTL)

    # Initialize empty structures with TTL
    r.expire(_k(code, "log"), TTL)
    r.expire(_k(code, "whispers"), TTL)
    r.expire(_k(code, "facts"), TTL)
    r.expire(_k(code, "npcs"), TTL)

    _bump(code)

    return jsonify({
        "code": code,
        "agents": [{"id": a["id"], "name": a["name"], "role": a["role"], "emoji": a["emoji"]}
                   for a in agents],
        "rulebook_chunks": len(rulebook_chunks),
    })


# ---- Poll state -----------------------------------------------------------
@agentic_dnd_bp.route("/tools/agentic-dnd/<code>/state", methods=["GET"])
@_require_auth
def get_state(code):
    code = code.upper()
    r = _r()

    # Cheap change-detection
    since = request.args.get("since")
    seq = int(r.get(_k(code, "_bump")) or 0)
    if since is not None and since.isdigit() and int(since) == seq:
        return jsonify({"seq": seq, "nochange": True})

    state = _state(code)
    if not state:
        return jsonify({"error": "Campaign not found."}), 404

    return jsonify(state)


# ---- Advance one turn (mock for Step 1) ------------------------------------
@agentic_dnd_bp.route("/tools/agentic-dnd/<code>/advance", methods=["POST"])
@_require_auth
def advance_turn(code):
    code = code.upper()
    r = _r()

    meta_raw = r.hgetall(_k(code, "meta"))
    if not meta_raw:
        return jsonify({"error": "Campaign not found."}), 404

    if str(session.get("user_id", "")) != meta_raw.get("creator_id", ""):
        return jsonify({"error": "Only the campaign creator can advance turns."}), 403

    status = meta_raw.get("status", "setup")
    if status != "active":
        return jsonify({"error": f"Campaign is {status}, not active."}), 400

    current_round = int(meta_raw.get("current_round", 1))
    current_index = int(meta_raw.get("current_agent_index", 0))

    if MAX_ROUNDS and current_round > MAX_ROUNDS:
        r.hset(_k(code, "meta"), "status", "ended")
        return jsonify({"error": f"Campaign ended — {MAX_ROUNDS} rounds reached."}), 400

    log_len = r.llen(_k(code, "log")) or 0
    if MAX_LOG_ENTRIES and log_len >= MAX_LOG_ENTRIES:
        r.hset(_k(code, "meta"), "status", "ended")
        return jsonify({"error": f"Campaign ended — {MAX_LOG_ENTRIES} turns reached."}), 400

    # Get ordered agent list
    agents_raw = r.hgetall(_k(code, "agents")) or {}
    agents = {}
    for aid, v in agents_raw.items():
        agents[aid] = json.loads(v)

    # Build ordered list (DM first, then alphabetical players)
    agent_order = sorted(agents.keys(),
                         key=lambda a: (0 if agents[a].get("role") == "dm" else 1, a))

    if not agent_order:
        return jsonify({"error": "No agents in campaign."}), 400

    if current_index >= len(agent_order):
        current_index = 0

    current_agent_id = agent_order[current_index]
    current_agent = agents[current_agent_id]

    # Pop pending whispers for this agent
    whispers_received = []
    whisper_count = r.llen(_k(code, "whispers")) or 0
    remaining_whispers = []
    for _ in range(whisper_count):
        raw = r.lpop(_k(code, "whispers"))
        if raw is None:
            break
        w = json.loads(raw)
        if w.get("to") == current_agent_id:
            whispers_received.append(w)
        else:
            remaining_whispers.append(w)
    # Push back non-matching whispers
    for w in remaining_whispers:
        r.rpush(_k(code, "whispers"), json.dumps(w))

    # --- Real inference via dnd_inference engine ---
    now = int(time.time())
    is_dm = current_agent["role"] == "dm"

    # Assemble full state (reuse _state but inject whispers for the turn)
    campaign_state = _state(code) or {}
    campaign_state["_whispers_for_turn"] = whispers_received

    try:
        from dnd_inference import run_agent_turn as _run_turn
        student_id = session.get("user_id")
        result = _run_turn(
            campaign_code=code,
            agent_id=current_agent_id,
            agent_config=current_agent,
            campaign_state=campaign_state,
            redis_client=r,
            student_id=student_id,
        )
        turn_message = result.get("message", "")
        turn_reasoning = result.get("reasoning")
        turn_tool_calls = result.get("tool_calls", [])
        turn_tokens_in = result.get("tokens_in", 0)
        turn_tokens_out = result.get("tokens_out", 0)
        turn_prompt_context = result.get("prompt_context")
        turn_context_estimate = result.get("context_estimate")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "DnD inference failed for %s in %s: %s", current_agent_id, code, e)
        turn_message = (
            "The DM pauses to think..."
            if is_dm
            else f"{current_agent['name']} hesitates for a moment..."
        )
        turn_reasoning = None
        turn_tool_calls = []
        turn_tokens_in = 0
        turn_tokens_out = 0
        turn_prompt_context = None
        turn_context_estimate = None

    msg_type = "narration" if is_dm else "action"

    log_entry = {
        "round": current_round,
        "agent_id": current_agent_id,
        "agent_name": current_agent.get("name", current_agent_id),
        "type": msg_type,
        "message": turn_message,
        "tool_calls": turn_tool_calls,
        "reasoning": turn_reasoning,
        "whispers_received": [{"from": w.get("from", "player"), "text": w.get("text", "")}
                               for w in whispers_received],
        "tokens_in": turn_tokens_in,
        "tokens_out": turn_tokens_out,
        "ts": now,
    }
    if turn_context_estimate:
        log_entry["context_estimate"] = turn_context_estimate
    if turn_prompt_context:
        log_entry["prompt_context"] = turn_prompt_context

    # Append to log (list grows rightward, oldest=index 0)
    r.rpush(_k(code, "log"), json.dumps(log_entry))

    # Advance turn
    next_index = current_index + 1
    next_round = current_round
    if next_index >= len(agent_order):
        next_index = 0
        next_round = current_round + 1

    r.hset(_k(code, "meta"), mapping={
        "current_agent_index": str(next_index),
        "current_round": str(next_round),
    })

    _bump(code)

    return jsonify({
        "ok": True,
        "turn": log_entry,
        "next_round": next_round,
        "next_agent_index": next_index,
        "next_agent_id": agent_order[next_index] if agent_order else None,
    })


# ---- Whisper to an agent ---------------------------------------------------
@agentic_dnd_bp.route("/tools/agentic-dnd/<code>/whisper", methods=["POST"])
@_require_auth
def send_whisper(code):
    code = code.upper()
    r = _r()

    meta_raw = r.hgetall(_k(code, "meta"))
    if not meta_raw:
        return jsonify({"error": "Campaign not found."}), 404

    if str(session.get("user_id", "")) != meta_raw.get("creator_id", ""):
        return jsonify({"error": "Only the campaign creator can send whispers."}), 403

    data = request.get_json(silent=True) or {}
    agent_id = _clean(data.get("agent_id"), 40)
    text = _clean(data.get("text"), 500)

    if not agent_id or not text:
        return jsonify({"error": "agent_id and text are required."}), 400

    # Verify agent exists
    if not r.hexists(_k(code, "agents"), agent_id):
        return jsonify({"error": f"Agent '{agent_id}' not found."}), 404

    if _rate_limited(f"whisper:{code}", _rate_ident(), 30, 60):
        return jsonify({"error": "Too many whispers. Slow down."}), 429

    whisper = {
        "from": "player",
        "to": agent_id,
        "text": text,
        "ts": int(time.time()),
    }
    r.rpush(_k(code, "whispers"), json.dumps(whisper))
    _bump(code)

    return jsonify({"ok": True, "whisper": whisper})


# ---- Export campaign log ---------------------------------------------------
@agentic_dnd_bp.route("/tools/agentic-dnd/<code>/export", methods=["GET"])
@_require_auth
def export_campaign(code):
    code = code.upper()
    state = _state(code)
    if not state:
        return jsonify({"error": "Campaign not found."}), 404

    is_creator = str(session.get("user_id", "")) == state.get("creator_id", "")

    agents_export = []
    for aid in state.get("agent_order", []):
        a = state["agents"].get(aid, {})
        agent_data = {
            "id": a.get("id", aid),
            "role": a.get("role", "player"),
            "name": a.get("name", ""),
            "tools": a.get("tools", []),
        }
        if is_creator:
            agent_data["system_prompt"] = a.get("system_prompt", "")
        agents_export.append(agent_data)

    export = {
        "campaign": state.get("name", ""),
        "code": code,
        "agents": agents_export,
        "turns": state.get("log", []),
        "facts": state.get("facts", {}),
        "npcs": state.get("npcs", {}),
        "total_rounds": state.get("current_round", 0),
        "exported_at": int(time.time()),
    }

    return jsonify(export)


# ---- Pause / Resume / End -------------------------------------------------
@agentic_dnd_bp.route("/tools/agentic-dnd/<code>/status", methods=["POST"])
@_require_auth
def set_status(code):
    code = code.upper()
    r = _r()

    meta_raw = r.hgetall(_k(code, "meta"))
    if not meta_raw:
        return jsonify({"error": "Campaign not found."}), 404

    # Only the creator can change status
    if str(session.get("user_id", "")) != meta_raw.get("creator_id", ""):
        return jsonify({"error": "Only the campaign creator can change status."}), 403

    data = request.get_json(silent=True) or {}
    new_status = data.get("status", "")
    if new_status not in ("active", "paused", "ended"):
        return jsonify({"error": "Status must be active, paused, or ended."}), 400

    r.hset(_k(code, "meta"), "status", new_status)
    _bump(code)

    return jsonify({"ok": True, "status": new_status})
