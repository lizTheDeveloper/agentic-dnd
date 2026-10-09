"""Agentic D&D — HTTP routes.

Students drop in markdown character sheets (1 DM + 3-5 players) and watch
AI agents interact in a turn-based campaign with tool use, whispers, and
chain-of-thought.  State lives in Redis (see store.py for the key layout).

  GET  /tools/agentic-dnd                  the single-page app
  POST /tools/agentic-dnd/create           new campaign from character sheets
  GET  /tools/agentic-dnd/<code>/state     poll (cheap no-change via ?since=)
  POST /tools/agentic-dnd/<code>/advance   run the next agent's turn
  POST /tools/agentic-dnd/<code>/whisper   private message to one agent
  GET  /tools/agentic-dnd/<code>/export    full log as JSON
  POST /tools/agentic-dnd/<code>/status    pause / resume / end
"""

import json
import logging
import os
import time

from flask import Blueprint, Response, jsonify, request, session

import store
from agent import run_agent_turn
from auth import rate_ident, rate_limited, require_session
from campaign import (allocate_code, build_agents, clean, load_agents,
                      load_state, save_new_campaign, take_whispers_for)
from store import key

logger = logging.getLogger(__name__)

agentic_dnd_bp = Blueprint("agentic_dnd", __name__)

MAX_MARKDOWN_BYTES = 64_000  # per agent system prompt (generous — models have large context)
MAX_LOG_ENTRIES = 0  # 0 = unlimited — sessions can run as long as the student wants
MAX_ROUNDS = 0  # 0 = unlimited — context compaction handles long sessions gracefully
APP_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "static", "index.html")


def _is_creator(meta):
    return str(session.get("user_id", "")) == meta.get("creator_id", "")


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
@require_session
def create_campaign():
    if rate_limited("create", rate_ident(), 10, 3600):
        return jsonify({"error": "Too many campaigns started. Take a breather."}), 429

    data = request.get_json(silent=True) or {}
    campaign_name = clean(data.get("name"), 100) or "Untitled Campaign"
    dm_markdown = (data.get("dm_markdown") or "").strip()
    character_markdowns = data.get("characters") or []

    # Rulebook chunks uploaded by the student (chunked in the browser)
    rulebook_chunks = data.get("rulebook_chunks") or []
    rulebook_name = clean(data.get("rulebook_name"), 100) or ""

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

    r = store.redis_client
    code = allocate_code(r)
    if not code:
        return jsonify({"error": "Could not allocate a campaign code. Try again."}), 503

    agents = build_agents(
        dm_markdown, character_markdowns,
        # Name / emoji overrides sent by the client
        dm_name=clean(data.get("dm_name"), 80) or "",
        dm_emoji=clean(data.get("dm_emoji"), 10) or "",
        char_names=data.get("char_names") or [],
        char_emojis=data.get("char_emojis") or [],
    )
    save_new_campaign(
        r, code, name=campaign_name, creator_id=session.get("user_id", ""),
        agents=agents, rulebook_name=rulebook_name,
        rulebook_chunks=rulebook_chunks,
    )
    store.bump(code)

    return jsonify({
        "code": code,
        "agents": [{"id": a["id"], "name": a["name"], "role": a["role"], "emoji": a["emoji"]}
                   for a in agents],
        "rulebook_chunks": len(rulebook_chunks),
    })


# ---- Poll state -----------------------------------------------------------
@agentic_dnd_bp.route("/tools/agentic-dnd/<code>/state", methods=["GET"])
@require_session
def get_state(code):
    code = code.upper()
    r = store.redis_client

    # Cheap change-detection
    since = request.args.get("since")
    seq = int(r.get(key(code, "_bump")) or 0)
    if since is not None and since.isdigit() and int(since) == seq:
        return jsonify({"seq": seq, "nochange": True})

    state = load_state(code)
    if not state:
        return jsonify({"error": "Campaign not found."}), 404

    return jsonify(state)


# ---- Advance one turn -----------------------------------------------------
@agentic_dnd_bp.route("/tools/agentic-dnd/<code>/advance", methods=["POST"])
@require_session
def advance_turn(code):
    code = code.upper()
    r = store.redis_client

    meta = r.hgetall(key(code, "meta"))
    if not meta:
        return jsonify({"error": "Campaign not found."}), 404

    if not _is_creator(meta):
        return jsonify({"error": "Only the campaign creator can advance turns."}), 403

    status = meta.get("status", "setup")
    if status != "active":
        return jsonify({"error": f"Campaign is {status}, not active."}), 400

    current_round = int(meta.get("current_round", 1))
    current_index = int(meta.get("current_agent_index", 0))

    if MAX_ROUNDS and current_round > MAX_ROUNDS:
        r.hset(key(code, "meta"), "status", "ended")
        return jsonify({"error": f"Campaign ended — {MAX_ROUNDS} rounds reached."}), 400

    log_len = r.llen(key(code, "log")) or 0
    if MAX_LOG_ENTRIES and log_len >= MAX_LOG_ENTRIES:
        r.hset(key(code, "meta"), "status", "ended")
        return jsonify({"error": f"Campaign ended — {MAX_LOG_ENTRIES} turns reached."}), 400

    # Whose turn is it?  (DM first, then players alphabetically)
    agents, agent_order = load_agents(r, code)
    if not agent_order:
        return jsonify({"error": "No agents in campaign."}), 400

    if current_index >= len(agent_order):
        current_index = 0

    current_agent_id = agent_order[current_index]
    current_agent = agents[current_agent_id]
    whispers_received = take_whispers_for(r, code, current_agent_id)

    now = int(time.time())
    is_dm = current_agent["role"] == "dm"

    # --- The agent takes its turn (agent.py) ---
    try:
        result = run_agent_turn(code, current_agent_id, current_agent,
                                whispers_received, r)
        turn_message = result.get("message", "")
        turn_reasoning = result.get("reasoning")
        turn_tool_calls = result.get("tool_calls", [])
        turn_tokens_in = result.get("tokens_in", 0)
        turn_tokens_out = result.get("tokens_out", 0)
        turn_prompt_context = result.get("prompt_context")
        turn_context_estimate = result.get("context_estimate")
    except Exception as e:
        logger.warning("DnD inference failed for %s in %s: %s", current_agent_id, code, e)
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

    log_entry = {
        "round": current_round,
        "agent_id": current_agent_id,
        "agent_name": current_agent.get("name", current_agent_id),
        "type": "narration" if is_dm else "action",
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
    r.rpush(key(code, "log"), json.dumps(log_entry))

    # Advance the turn pointer
    next_index = current_index + 1
    next_round = current_round
    if next_index >= len(agent_order):
        next_index = 0
        next_round = current_round + 1

    r.hset(key(code, "meta"), mapping={
        "current_agent_index": str(next_index),
        "current_round": str(next_round),
    })

    store.bump(code)

    return jsonify({
        "ok": True,
        "turn": log_entry,
        "next_round": next_round,
        "next_agent_index": next_index,
        "next_agent_id": agent_order[next_index] if agent_order else None,
    })


# ---- Whisper to an agent ---------------------------------------------------
@agentic_dnd_bp.route("/tools/agentic-dnd/<code>/whisper", methods=["POST"])
@require_session
def send_whisper(code):
    code = code.upper()
    r = store.redis_client

    meta = r.hgetall(key(code, "meta"))
    if not meta:
        return jsonify({"error": "Campaign not found."}), 404

    if not _is_creator(meta):
        return jsonify({"error": "Only the campaign creator can send whispers."}), 403

    data = request.get_json(silent=True) or {}
    agent_id = clean(data.get("agent_id"), 40)
    text = clean(data.get("text"), 500)

    if not agent_id or not text:
        return jsonify({"error": "agent_id and text are required."}), 400

    # Verify agent exists
    if not r.hexists(key(code, "agents"), agent_id):
        return jsonify({"error": f"Agent '{agent_id}' not found."}), 404

    if rate_limited(f"whisper:{code}", rate_ident(), 30, 60):
        return jsonify({"error": "Too many whispers. Slow down."}), 429

    whisper = {
        "from": "player",
        "to": agent_id,
        "text": text,
        "ts": int(time.time()),
    }
    r.rpush(key(code, "whispers"), json.dumps(whisper))
    store.bump(code)

    return jsonify({"ok": True, "whisper": whisper})


# ---- Export campaign log ---------------------------------------------------
@agentic_dnd_bp.route("/tools/agentic-dnd/<code>/export", methods=["GET"])
@require_session
def export_campaign(code):
    code = code.upper()
    state = load_state(code)
    if not state:
        return jsonify({"error": "Campaign not found."}), 404

    is_creator = _is_creator(state)

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
@require_session
def set_status(code):
    code = code.upper()
    r = store.redis_client

    meta = r.hgetall(key(code, "meta"))
    if not meta:
        return jsonify({"error": "Campaign not found."}), 404

    # Only the creator can change status
    if not _is_creator(meta):
        return jsonify({"error": "Only the campaign creator can change status."}), 403

    data = request.get_json(silent=True) or {}
    new_status = data.get("status", "")
    if new_status not in ("active", "paused", "ended"):
        return jsonify({"error": "Status must be active, paused, or ended."}), 400

    r.hset(key(code, "meta"), "status", new_status)
    store.bump(code)

    return jsonify({"ok": True, "status": new_status})
