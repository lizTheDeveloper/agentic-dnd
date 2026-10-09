"""Agentic D&D — inference engine with context compaction.

Handles LLM calls for agent turns, tool execution, context assembly,
and context compaction for long-running campaigns.

The compaction system keeps campaigns playable past the context window:
  1. estimate_context_tokens()  — count how full the window is
  2. should_compact()           — trigger when > 70% full
  3. extract_facts_from_history() — DM extracts facts via remember_fact()
  4. summarize_history()        — DM writes a narrative recap (~500 tokens)
  5. compact_campaign()         — orchestrates the full cycle

Compaction is wired into run_agent_turn() so it fires automatically.
"""

import json
import logging
import math
import os
import random
import time
import uuid
from datetime import datetime, timezone

import requests as http_requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
DND_MODEL = os.environ.get("DND_INFERENCE_MODEL", "openrouter/auto")
DND_MAX_TOKENS = int(os.environ.get("DND_INFERENCE_MAX_TOKENS", "4096"))
DND_TIMEOUT = int(os.environ.get("DND_INFERENCE_TIMEOUT", "60"))
DND_MODEL_CONTEXT_WINDOW = int(os.environ.get("DND_MODEL_CONTEXT_WINDOW", "32000"))
DND_COMPACTION_KEEP_ROUNDS = int(os.environ.get("DND_COMPACTION_KEEP_ROUNDS", "3"))

# Langfuse (same lightweight REST pattern as mutual_aid/langfuse_trace.py)
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST",
                                "https://langfuse.multiversestudios.xyz")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")

# Spend tracking
_REDIS_DND_SPEND_KEY = "dnd:spend:{code}"

# ---------------------------------------------------------------------------
# Redis key helper (mirrors agentic_dnd._k)
# ---------------------------------------------------------------------------

def _k(code, suffix=""):
    return f"dnd:{code}" + (f":{suffix}" if suffix else "")


# ---------------------------------------------------------------------------
# Langfuse tracing (never raises — tracing must not break gameplay)
# ---------------------------------------------------------------------------
_NOOP_LOGGED = False


def _iso(ts):
    return (datetime.fromtimestamp(ts, tz=timezone.utc)
            .isoformat().replace("+00:00", "Z"))


def _trace_generation(name, model, input_data, output_data, *,
                      metadata=None, start_time, end_time):
    """Record one LLM generation to Langfuse.  Silent no-op if keys unset."""
    global _NOOP_LOGGED
    try:
        if not (LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY):
            if not _NOOP_LOGGED:
                logger.debug("Langfuse tracing disabled (keys unset)")
                _NOOP_LOGGED = True
            return
        trace_id = str(uuid.uuid4())
        gen_id = str(uuid.uuid4())
        start_iso = _iso(start_time)
        end_iso = _iso(end_time)
        now_iso = _iso(time.time())
        batch = [
            {
                "id": str(uuid.uuid4()),
                "type": "trace-create",
                "timestamp": now_iso,
                "body": {
                    "id": trace_id, "name": name,
                    "timestamp": start_iso,
                    "input": input_data, "output": output_data,
                    "metadata": metadata or {},
                },
            },
            {
                "id": str(uuid.uuid4()),
                "type": "generation-create",
                "timestamp": now_iso,
                "body": {
                    "id": gen_id, "traceId": trace_id,
                    "type": "GENERATION", "name": name, "model": model,
                    "startTime": start_iso, "endTime": end_iso,
                    "input": input_data, "output": output_data,
                    "level": "DEFAULT",
                    "metadata": metadata or {},
                },
            },
        ]
        http_requests.post(
            f"{LANGFUSE_HOST.rstrip('/')}/api/public/ingestion",
            json={"batch": batch},
            auth=(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY),
            timeout=5,
        )
    except Exception as exc:
        logger.debug("Langfuse trace failed (ignored): %s", exc)


# ---------------------------------------------------------------------------
# Spend tracking
# ---------------------------------------------------------------------------

def _estimate_cost_cents(usage):
    """Rough cost estimate.  OpenRouter returns usage but we use flat rates."""
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    # Generous flat rate: $1/M input, $3/M output
    input_cost = (prompt_tokens / 1_000_000) * 1.0
    output_cost = (completion_tokens / 1_000_000) * 3.0
    return round((input_cost + output_cost) * 100, 4)


def _add_spend(campaign_code, cost_cents, redis_client):
    """Accumulate inference spend for a campaign."""
    try:
        redis_client.incrbyfloat(
            _REDIS_DND_SPEND_KEY.format(code=campaign_code), cost_cents)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = {
    "roll_dice": {
        "type": "function",
        "function": {
            "name": "roll_dice",
            "description": "Roll dice and return the result. Use for ability checks, attacks, damage, saving throws.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sides": {"type": "integer", "description": "Number of sides (e.g. 20 for d20)"},
                    "count": {"type": "integer", "description": "Number of dice to roll", "default": 1},
                    "modifier": {"type": "integer", "description": "Bonus/penalty to add", "default": 0},
                },
                "required": ["sides"],
            },
        },
    },
    "create_npc": {
        "type": "function",
        "function": {
            "name": "create_npc",
            "description": "Create a new NPC in the world.  Use when introducing a named character.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "NPC name"},
                    "description": {"type": "string", "description": "Appearance, personality, motivation"},
                    "stats": {"type": "string", "description": "Key stats or abilities (freeform)"},
                },
                "required": ["name", "description"],
            },
        },
    },
    "lookup_rule": {
        "type": "function",
        "function": {
            "name": "lookup_rule",
            "description": "Look up a game rule or mechanic in the rulebook. Use this instead of inventing rules.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What rule or mechanic to look up"},
                },
                "required": ["query"],
            },
        },
    },
    "view_npc_sheet": {
        "type": "function",
        "function": {
            "name": "view_npc_sheet",
            "description": "View an NPC's full sheet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "npc_id": {"type": "string", "description": "The NPC identifier"},
                },
                "required": ["npc_id"],
            },
        },
    },
    "set_scene": {
        "type": "function",
        "function": {
            "name": "set_scene",
            "description": "Describe the current scene, setting the atmosphere and environment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Vivid scene description"},
                    "mood": {"type": "string", "description": "e.g. tense, peaceful, mysterious"},
                    "danger_level": {"type": "integer", "description": "0 (safe) to 5 (deadly)"},
                },
                "required": ["description"],
            },
        },
    },
    "resolve_action": {
        "type": "function",
        "function": {
            "name": "resolve_action",
            "description": "Resolve a player's declared action — determine outcome and narrate the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "What the player is trying to do"},
                    "difficulty": {"type": "integer", "description": "DC (difficulty class), 1-30"},
                    "modifier": {"type": "integer", "description": "Player's relevant modifier"},
                },
                "required": ["action"],
            },
        },
    },
    "remember_fact": {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": "Store an important world fact that should persist across the campaign.  Use for key plot points, NPC relationships, player decisions, world-state changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "The fact to remember"},
                    "category": {
                        "type": "string",
                        "description": "Category: plot, npc, location, item, player_decision, world_state",
                        "default": "world_state",
                    },
                },
                "required": ["fact"],
            },
        },
    },
    "recall_facts": {
        "type": "function",
        "function": {
            "name": "recall_facts",
            "description": "Recall stored world facts, optionally filtered by category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Filter by category (optional)"},
                },
            },
        },
    },
    "take_note": {
        "type": "function",
        "function": {
            "name": "take_note",
            "description": "Write a private note that only you can see.  Use to track your plans, suspicions, or strategies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "The note content"},
                    "tag": {"type": "string", "description": "Optional tag for organization"},
                },
                "required": ["note"],
            },
        },
    },
    "read_notes": {
        "type": "function",
        "function": {
            "name": "read_notes",
            "description": "Read your private notes, optionally filtered by tag.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Filter by tag (optional)"},
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _execute_tool_call(tool_name, args, campaign_code, agent_id, redis_client):
    """Execute a tool call and return the result string."""
    if tool_name == "roll_dice":
        sides = int(args.get("sides", 20))
        count = int(args.get("count", 1))
        modifier = int(args.get("modifier", 0))
        rolls = [random.randint(1, max(1, sides)) for _ in range(max(1, count))]
        total = sum(rolls) + modifier
        mod_str = f" + {modifier}" if modifier > 0 else (f" - {abs(modifier)}" if modifier < 0 else "")
        return f"Rolled {count}d{sides}{mod_str}: {rolls} = {total}"

    elif tool_name == "create_npc":
        npc_id = args.get("name", "npc").lower().replace(" ", "_")[:30]
        npc_data = {
            "name": args.get("name", "Unknown"),
            "description": args.get("description", ""),
            "stats": args.get("stats", ""),
            "created_by": agent_id,
            "created_at": int(time.time()),
        }
        redis_client.hset(_k(campaign_code, "npcs"), npc_id, json.dumps(npc_data))
        return f"NPC '{npc_data['name']}' created (id: {npc_id})."

    elif tool_name == "lookup_rule":
        query = args.get("query", "")
        return execute_lookup_rule(query, campaign_code, redis_client)

    elif tool_name == "view_npc_sheet":
        npc_id = args.get("npc_id", "")
        raw = redis_client.hget(_k(campaign_code, "npcs"), npc_id)
        if raw:
            npc = json.loads(raw)
            return json.dumps(npc, indent=2)
        return f"NPC '{npc_id}' not found."

    elif tool_name == "set_scene":
        desc = args.get("description", "")
        mood = args.get("mood", "neutral")
        danger = args.get("danger_level", 0)
        return f"Scene set. Mood: {mood}, Danger: {danger}/5."

    elif tool_name == "resolve_action":
        action = args.get("action", "")
        difficulty = int(args.get("difficulty", 10))
        modifier = int(args.get("modifier", 0))
        roll = random.randint(1, 20) + modifier
        success = roll >= difficulty
        return (f"Action: {action}. Roll: {roll} vs DC {difficulty}. "
                f"{'SUCCESS' if success else 'FAILURE'}.")

    elif tool_name == "remember_fact":
        fact_text = args.get("fact", "")
        category = args.get("category", "world_state")
        fact_id = str(uuid.uuid4())[:8]
        fact_data = {
            "fact": fact_text,
            "category": category,
            "remembered_by": agent_id,
            "round": _get_current_round(campaign_code, redis_client),
            "ts": int(time.time()),
        }
        redis_client.hset(_k(campaign_code, "facts"), fact_id, json.dumps(fact_data))
        return f"Fact remembered: {fact_text}"

    elif tool_name == "recall_facts":
        category = args.get("category")
        facts_raw = redis_client.hgetall(_k(campaign_code, "facts")) or {}
        facts = []
        for fid, v in facts_raw.items():
            f = json.loads(v)
            if category and f.get("category") != category:
                continue
            facts.append(f"[{f.get('category', '?')}] {f.get('fact', '')}")
        if not facts:
            return "No facts recorded yet." if not category else f"No facts in category '{category}'."
        return "\n".join(facts)

    elif tool_name == "take_note":
        note_text = args.get("note", "")
        tag = args.get("tag", "general")
        note_id = str(uuid.uuid4())[:8]
        note_data = {"note": note_text, "tag": tag, "ts": int(time.time())}
        redis_client.hset(
            _k(campaign_code, f"notes:{agent_id}"), note_id, json.dumps(note_data))
        return f"Note taken ({tag}): {note_text[:60]}..."

    elif tool_name == "read_notes":
        tag = args.get("tag")
        notes_raw = redis_client.hgetall(_k(campaign_code, f"notes:{agent_id}")) or {}
        notes = []
        for nid, v in notes_raw.items():
            n = json.loads(v)
            if tag and n.get("tag") != tag:
                continue
            notes.append(f"[{n.get('tag', '?')}] {n.get('note', '')}")
        if not notes:
            return "No notes yet." if not tag else f"No notes with tag '{tag}'."
        return "\n".join(notes)

    return f"Unknown tool: {tool_name}"


def _get_current_round(campaign_code, redis_client):
    """Read current round number from campaign meta."""
    try:
        val = redis_client.hget(_k(campaign_code, "meta"), "current_round")
        return int(val) if val else 1
    except Exception:
        return 1


# ---------------------------------------------------------------------------
# Rulebook RAG search
# ---------------------------------------------------------------------------

def execute_lookup_rule(query, campaign_code, redis_client):
    """Search the loaded rulebook for relevant chunks.

    Performs simple keyword + title matching against student-uploaded chunks
    stored in Redis.  Returns top 3 matches with content so the DM can
    quote the rule in its narration.
    """
    if not query:
        return "[Rule lookup: empty query]"

    # Load chunks from Redis hash
    raw_chunks = redis_client.hgetall(_k(campaign_code, "rulebook")) or {}
    if not raw_chunks:
        return (f"[Rule lookup: {query}] — No rulebook loaded for this campaign. "
                f"The Warden adjudicates based on their judgment.")

    query_lower = query.lower()
    query_words = set(query_lower.split())

    scored = []
    for chunk_id, chunk_json in raw_chunks.items():
        try:
            chunk = json.loads(chunk_json)
        except (json.JSONDecodeError, TypeError):
            continue

        title = (chunk.get("title") or "").lower()
        content = (chunk.get("content") or "").lower()
        full_text = title + " " + content

        # Score: title matches are worth more
        score = 0

        # Exact query substring in title = high relevance
        if query_lower in title:
            score += 10

        # Exact query substring in content
        if query_lower in content:
            score += 5

        # Individual word matches
        for word in query_words:
            if len(word) < 3:
                continue
            if word in title:
                score += 3
            count = content.count(word)
            score += min(count, 3)  # cap per-word contribution

        if score > 0:
            scored.append((score, chunk_id, chunk))

    # Sort by score descending, take top 3
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:3]

    if not top:
        return (f"[Rule lookup: {query}] — No matching rules found in the "
                f"rulebook.  The Warden adjudicates based on their judgment.")

    parts = [f"[Rulebook lookup: \"{query}\"]"]
    for score, cid, chunk in top:
        title = chunk.get("title", cid)
        content = chunk.get("content", "")
        # Truncate very long chunks to stay within token budget
        if len(content) > 600:
            content = content[:597] + "..."
        parts.append(f"\n--- {title} (relevance: {score}) ---\n{content}")

    return "\n".join(parts)


def _get_rulebook_name(campaign_code, redis_client):
    """Return the rulebook name from campaign meta, or None."""
    try:
        return redis_client.hget(_k(campaign_code, "meta"), "rulebook_name")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def _get_tool_defs(tool_names):
    """Return OpenAI-format tool definitions for the given tool names."""
    return [TOOL_SCHEMAS[t] for t in tool_names if t in TOOL_SCHEMAS]


def build_agent_context(agent_config, campaign_code, redis_client):
    """Assemble system prompt, messages, facts, notes, and tools for an agent.

    Returns: {system_prompt, messages, facts_text, notes_text, tools, tools_json}
    """
    agent_id = agent_config["id"]
    role = agent_config.get("role", "player")
    tool_names = agent_config.get("tools", [])

    # System prompt
    system_prompt = agent_config.get("system_prompt", "")

    # Rulebook instruction for DM
    rulebook_instruction = ""
    if role == "dm":
        rulebook_name = _get_rulebook_name(campaign_code, redis_client)
        if rulebook_name:
            rulebook_instruction = (
                f"\n\nYou have access to the [{rulebook_name}] rulebook via "
                f"the lookup_rule tool. When a player attempts an action that "
                f"involves game mechanics (combat, saves, magic, inventory), "
                f"ALWAYS call lookup_rule first rather than inventing rules. "
                f"Quote the rule in your narration when relevant."
            )

    memory_instruction = ""
    if role == "dm":
        memory_instruction = (
            "\n\nYOUR TURN PROCEDURE — follow this every turn:"
            "\n1. ORIENT: Call recall_facts() and read_notes() to load what "
            "you know. Do not guess or invent past events. Never contradict "
            "a stored fact."
            "\n2. RESOLVE: If any player declared an action since your last "
            "turn, resolve its outcome now (use resolve_action if a check is "
            "needed). Only resolve actions players already stated."
            "\n3. NARRATE: Describe what happens in the world — NPC "
            "reactions, environmental changes, consequences. Be vivid but "
            "concise. Use set_scene for major location changes."
            "\n4. RECORD: Call remember_fact() for each important event, "
            "item, NPC, or decision this turn. Categories: inventory, "
            "location, event, relationship, world. Use take_note() for "
            "your private plans (plot threads, hidden motives, upcoming "
            "events)."
            "\n5. INVITE: End with a clear situation that asks the player "
            "characters what they do. Describe what they see, hear, or "
            "face — then stop. Let them decide."
            "\n\nCRITICAL — Player Agency: NEVER describe what a player "
            "character does, says, thinks, or feels. NEVER take actions on "
            "behalf of a player character. You control the world and NPCs; "
            "the players control their characters."
        )

    role_instruction = (
        "\n\nYou are the Dungeon Master. You control the world, NPCs, and "
        "story — the players control their characters. Follow your turn "
        "procedure above on every turn."
        if role == "dm" else
        f"\n\nYou are playing as {agent_config.get('name', 'a character')}. "
        f"Stay in character. Be bold and decisive — declare specific "
        f"actions, speak dialogue in first person. Use roll_dice() when "
        f"you attempt something risky or uncertain. Use take_note() for "
        f"things you want to remember."
    )
    full_system = system_prompt + rulebook_instruction + memory_instruction + role_instruction

    # Conversation history from the log
    log_raw = redis_client.lrange(_k(campaign_code, "log"), 0, -1) or []
    messages = []
    for entry_raw in log_raw:
        entry = json.loads(entry_raw)

        # Handle compaction entries — include summary as context
        if entry.get("type") == "compaction":
            summary = entry.get("summary", "")
            if summary:
                messages.append({
                    "role": "assistant" if role == "dm" else "user",
                    "content": f"[Campaign Summary — Previously:]\n{summary}",
                })
            continue

        # Regular turn entries
        entry_agent = entry.get("agent_id", "")
        entry_message = entry.get("message", "")
        entry_type = entry.get("type", "action")

        if entry_agent == agent_id:
            # This agent's own previous messages
            messages.append({"role": "assistant", "content": entry_message})
        else:
            # Other agents' messages appear as observations
            agent_name = entry.get("agent_name", entry_agent)
            prefix = f"[{agent_name}]" if agent_name else ""
            messages.append({
                "role": "user",
                "content": f"{prefix} {entry_message}".strip(),
            })

    # World facts
    facts_raw = redis_client.hgetall(_k(campaign_code, "facts")) or {}
    facts_lines = []
    for fid, v in facts_raw.items():
        f = json.loads(v)
        facts_lines.append(f"- [{f.get('category', '?')}] {f.get('fact', '')}")
    facts_text = ""
    if facts_lines:
        facts_text = "World Facts:\n" + "\n".join(facts_lines)

    # Agent's private notes
    notes_raw = redis_client.hgetall(_k(campaign_code, f"notes:{agent_id}")) or {}
    notes_lines = []
    for nid, v in notes_raw.items():
        n = json.loads(v)
        notes_lines.append(f"- [{n.get('tag', '?')}] {n.get('note', '')}")
    notes_text = ""
    if notes_lines:
        notes_text = "Your Private Notes:\n" + "\n".join(notes_lines)

    # Append facts and notes to system prompt
    if facts_text:
        full_system += f"\n\n{facts_text}"
    if notes_text:
        full_system += f"\n\n{notes_text}"

    # Tools
    tools = _get_tool_defs(tool_names)
    tools_json = json.dumps(tools)

    return {
        "system_prompt": full_system,
        "messages": messages,
        "facts_text": facts_text,
        "notes_text": notes_text,
        "tools": tools,
        "tools_json": tools_json,
    }


# ---------------------------------------------------------------------------
# Token estimation & compaction trigger
# ---------------------------------------------------------------------------

def estimate_context_tokens(agent_config, history, facts, notes, tools):
    """Estimate the token count for an agent's context.

    Token estimate: ceil(len(text) / 4) for each component.

    Returns: {total: int, breakdown: {system, tools, history, facts, notes}}
    """
    def _count(text):
        return math.ceil(len(text) / 4) if text else 0

    system_tokens = _count(agent_config.get("system_prompt", ""))

    # Tools: serialize the tool definitions
    if isinstance(tools, str):
        tools_text = tools
    elif isinstance(tools, list):
        tools_text = json.dumps(tools)
    else:
        tools_text = ""
    tools_tokens = _count(tools_text)

    # History: serialize all messages
    if isinstance(history, str):
        history_text = history
    elif isinstance(history, list):
        history_text = json.dumps(history)
    else:
        history_text = ""
    history_tokens = _count(history_text)

    facts_tokens = _count(facts if isinstance(facts, str) else json.dumps(facts or ""))
    notes_tokens = _count(notes if isinstance(notes, str) else json.dumps(notes or ""))

    breakdown = {
        "system": system_tokens,
        "tools": tools_tokens,
        "history": history_tokens,
        "facts": facts_tokens,
        "notes": notes_tokens,
    }
    return {
        "total": sum(breakdown.values()),
        "breakdown": breakdown,
    }


def should_compact(token_estimate, model_window=None):
    """Return True when context exceeds 70% of the model's window."""
    if model_window is None:
        model_window = DND_MODEL_CONTEXT_WINDOW
    total = token_estimate if isinstance(token_estimate, int) else token_estimate.get("total", 0)
    return total > model_window * 0.7


# ---------------------------------------------------------------------------
# Fact extraction (compaction phase 1)
# ---------------------------------------------------------------------------

def extract_facts_from_history(campaign_code, rounds_to_extract, redis_client):
    """Extract structured facts from old rounds via a DM inference call.

    Makes ONE call to the DM agent with remember_fact as the only tool.
    Parses tool calls from the response and executes each against Redis.

    Returns: list of extracted facts [{fact, category, round}]
    """
    if not rounds_to_extract:
        return []

    # Build the history text from the rounds
    history_text = _format_rounds_for_compaction(rounds_to_extract)

    system = (
        "You are the Dungeon Master reviewing your campaign notes. "
        "Extract ALL important facts from the following campaign history "
        "into structured facts. Call remember_fact() for EACH fact — "
        "plot points, NPC relationships, player decisions, location details, "
        "items found, world-state changes. Be thorough; anything not "
        "extracted will be lost when the history is summarized."
    )

    messages = [{"role": "user", "content": history_text}]
    tools = [TOOL_SCHEMAS["remember_fact"]]

    start_time = time.time()
    extracted = []

    try:
        result = _call_openrouter(system, messages, tools=tools,
                                  max_tokens=DND_MAX_TOKENS * 2)
        end_time = time.time()

        # Parse tool calls from response
        if result and result.get("choices"):
            choice = result["choices"][0]
            msg = choice.get("message", {})

            # Handle tool_calls in the response
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                fn = tc.get("function", {})
                fn_name = fn.get("name", "")
                if fn_name == "remember_fact":
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    fact_text = args.get("fact", "")
                    category = args.get("category", "world_state")
                    if fact_text:
                        # Execute against Redis (additive)
                        _execute_tool_call(
                            "remember_fact", args, campaign_code, "dm_compaction",
                            redis_client)
                        extracted.append({
                            "fact": fact_text,
                            "category": category,
                            "round": "compaction",
                        })

        # Track cost
        usage = result.get("usage", {}) if result else {}
        cost = _estimate_cost_cents(usage)
        _add_spend(campaign_code, cost, redis_client)

        # Langfuse trace
        _trace_generation(
            "compaction_extract", DND_MODEL,
            {"system": system, "history_length": len(history_text)},
            {"facts_extracted": len(extracted)},
            metadata={
                "campaign_code": campaign_code,
                "rounds_extracted": len(rounds_to_extract),
            },
            start_time=start_time, end_time=end_time,
        )

    except Exception as e:
        logger.warning("Fact extraction failed: %s", e)

    return extracted


# ---------------------------------------------------------------------------
# History summarization (compaction phase 2)
# ---------------------------------------------------------------------------

def summarize_history(campaign_code, rounds_to_summarize, redis_client):
    """Summarize old rounds into a narrative recap (~500 tokens).

    Makes ONE inference call to the DM agent.
    Returns: summary text (string).
    """
    if not rounds_to_summarize:
        return ""

    history_text = _format_rounds_for_compaction(rounds_to_summarize)

    system = (
        "You are the Dungeon Master. Summarize the campaign so far in "
        "approximately 500 tokens for the party's records. Write in your "
        "narrative voice — this summary will be read as 'Previously on our "
        "adventure...' at the start of the next session. Include key events, "
        "important decisions the party made, and unresolved threads. Do NOT "
        "use bullet points — write flowing narrative prose."
    )

    messages = [{"role": "user", "content": history_text}]

    start_time = time.time()
    summary = ""

    try:
        result = _call_openrouter(system, messages, tools=None,
                                  max_tokens=700)
        end_time = time.time()

        if result and result.get("choices"):
            msg = result["choices"][0].get("message", {})
            summary = msg.get("content", "") or ""

        # Track cost
        usage = result.get("usage", {}) if result else {}
        cost = _estimate_cost_cents(usage)
        _add_spend(campaign_code, cost, redis_client)

        # Langfuse trace
        _trace_generation(
            "compaction_summarize", DND_MODEL,
            {"system": system, "history_length": len(history_text)},
            {"summary_length": len(summary)},
            metadata={"campaign_code": campaign_code},
            start_time=start_time, end_time=end_time,
        )

    except Exception as e:
        logger.warning("History summarization failed: %s", e)
        summary = "(Summary unavailable — previous rounds were compacted.)"

    return summary


# ---------------------------------------------------------------------------
# Full compaction orchestrator
# ---------------------------------------------------------------------------

def compact_campaign(campaign_code, redis_client):
    """Orchestrate full context compaction.

    1. Read the full log
    2. Determine which rounds to compact (all except last N rounds)
    3. Extract facts from those rounds
    4. Summarize those rounds
    5. Replace compacted entries with a single compaction entry
    6. Write back to Redis

    Returns: the compaction entry dict (for the UI to display), or None.
    """
    # 1. Read full log
    log_raw = redis_client.lrange(_k(campaign_code, "log"), 0, -1) or []
    if not log_raw:
        return None

    log_entries = [json.loads(x) for x in log_raw]

    # Separate existing compaction entries from turn entries
    turn_entries = [e for e in log_entries if e.get("type") != "compaction"]
    compaction_entries = [e for e in log_entries if e.get("type") == "compaction"]

    if len(turn_entries) <= DND_COMPACTION_KEEP_ROUNDS:
        return None  # Not enough turns to compact

    # 2. Determine rounds to compact — group entries by round number
    rounds_present = sorted(set(e.get("round", 0) for e in turn_entries))
    if len(rounds_present) <= DND_COMPACTION_KEEP_ROUNDS:
        return None

    # Keep the last N rounds, compact the rest
    rounds_to_keep = set(rounds_present[-DND_COMPACTION_KEEP_ROUNDS:])
    rounds_to_compact = [r for r in rounds_present if r not in rounds_to_keep]

    entries_to_compact = [e for e in turn_entries if e.get("round", 0) in set(rounds_to_compact)]
    entries_to_keep = [e for e in turn_entries if e.get("round", 0) in rounds_to_keep]

    if not entries_to_compact:
        return None

    # Measure tokens before compaction
    tokens_before = math.ceil(len(json.dumps(log_entries)) / 4)

    # 3. Extract facts from those rounds
    extracted_facts = extract_facts_from_history(
        campaign_code, entries_to_compact, redis_client)

    # 4. Summarize those rounds
    summary = summarize_history(
        campaign_code, entries_to_compact, redis_client)

    # 5. Build compaction entry
    current_round = _get_current_round(campaign_code, redis_client)
    compaction_entry = {
        "type": "compaction",
        "round": current_round,
        "summary": f"Previously: {summary}",
        "facts_extracted": extracted_facts,
        "rounds_compacted": rounds_to_compact,
        "tokens_before": tokens_before,
        "tokens_after": 0,  # filled below
        "ts": int(time.time()),
    }

    # 6. Build new log: existing compaction entries + new compaction + kept entries
    new_log = compaction_entries + [compaction_entry] + entries_to_keep
    compaction_entry["tokens_after"] = math.ceil(len(json.dumps(new_log)) / 4)

    # 7. Write back to Redis (DEL + RPUSH)
    log_key = _k(campaign_code, "log")
    redis_client.delete(log_key)
    for entry in new_log:
        redis_client.rpush(log_key, json.dumps(entry))

    # 8. Bump campaign state
    redis_client.incr(_k(campaign_code, "_bump"))

    logger.info(
        "Compacted campaign %s: %d rounds -> summary (%d->%d est. tokens), "
        "%d facts extracted",
        campaign_code, len(rounds_to_compact),
        tokens_before, compaction_entry["tokens_after"],
        len(extracted_facts),
    )

    return compaction_entry


# ---------------------------------------------------------------------------
# OpenRouter call (shared by turn inference and compaction)
# ---------------------------------------------------------------------------

def _call_openrouter(system, messages, *, tools=None, max_tokens=None):
    """Make a single OpenRouter chat completion call.

    Returns the raw JSON response dict, or None on failure.
    """
    if not OPENROUTER_KEY:
        logger.warning("OPENROUTER_API_KEY not set — inference disabled")
        return None

    body = {
        "model": DND_MODEL,
        "temperature": 0.7,
        "messages": [{"role": "system", "content": system}] + messages,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://themultiverse.school",
        "X-Title": "Multiverse School Agentic D&D",
    }

    resp = http_requests.post(
        OPENROUTER_URL, json=body, headers=headers, timeout=DND_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_rounds_for_compaction(entries):
    """Format log entries into readable text for the compaction LLM calls."""
    lines = []
    current_round = None
    for entry in entries:
        rnd = entry.get("round", "?")
        if rnd != current_round:
            current_round = rnd
            lines.append(f"\n--- Round {rnd} ---")

        agent_name = entry.get("agent_name", entry.get("agent_id", "Unknown"))
        msg = entry.get("message", "")
        entry_type = entry.get("type", "action")

        lines.append(f"[{agent_name}] ({entry_type}): {msg}")

        # Include tool call results for context
        for tc in entry.get("tool_calls", []):
            lines.append(f"  -> {tc.get('name', '?')}: {tc.get('result', '')}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent turn execution
# ---------------------------------------------------------------------------

def run_agent_turn(campaign_code, agent_id, agent_config,
                   whispers=None, redis_client=None, *,
                   campaign_state=None, student_id=None):
    """Run one agent's turn: build context, call LLM, execute tools.

    Checks for compaction before building context.

    Accepts either ``whispers`` (list) directly or ``campaign_state``
    (dict with ``_whispers_for_turn`` key) — the latter is what
    ``agentic_dnd.py`` passes.

    Returns: log_entry dict (same shape as the mock in agentic_dnd.py).
    """
    # Normalise whispers from either calling convention
    if whispers is None and campaign_state is not None:
        whispers = campaign_state.get("_whispers_for_turn") or []
    elif whispers is None:
        whispers = []

    # --- Compaction check (before building context) ---
    context = build_agent_context(agent_config, campaign_code, redis_client)
    token_estimate = estimate_context_tokens(
        agent_config,
        context["messages"],
        context["facts_text"],
        context["notes_text"],
        context["tools"],
    )

    compaction_entry = None
    if should_compact(token_estimate):
        compaction_entry = compact_campaign(campaign_code, redis_client)
        if compaction_entry:
            # Rebuild context after compaction
            context = build_agent_context(
                agent_config, campaign_code, redis_client)
            token_estimate = estimate_context_tokens(
                agent_config,
                context["messages"],
                context["facts_text"],
                context["notes_text"],
                context["tools"],
            )

    # --- Build messages for the LLM ---
    llm_messages = list(context["messages"])

    # Inject whispers as a system-level aside
    if whispers:
        whisper_text = "\n".join(
            f"[Whisper from {w.get('from', 'unknown')}]: {w.get('text', '')}"
            for w in whispers
        )
        llm_messages.append({
            "role": "user",
            "content": f"[Private whispers for you — do not reveal these directly]\n{whisper_text}",
        })

    # Prompt the agent to act
    current_round = _get_current_round(campaign_code, redis_client)
    if agent_config.get("role") == "dm":
        if current_round == 1 and not context["messages"]:
            llm_messages.append({
                "role": "user",
                "content": (
                    "Begin the campaign. Use set_scene to establish the "
                    "opening location, then narrate the scene the "
                    "adventurers find themselves in. End by describing "
                    "what the characters see and asking what they do."
                ),
            })
        else:
            llm_messages.append({
                "role": "user",
                "content": (
                    "It is your turn. Follow your turn procedure: "
                    "recall_facts → resolve declared player actions → "
                    "narrate the world → remember_fact for anything "
                    "important → end with a situation that invites the "
                    "players to act."
                ),
            })
    else:
        llm_messages.append({
            "role": "user",
            "content": (
                "It is your turn. Declare what your character does — be "
                "specific and decisive. Speak dialogue in first person. "
                "If you attempt something risky, use roll_dice."
            ),
        })

    # --- Call OpenRouter with multi-round tool loop ---
    max_tool_rounds = int(os.environ.get("DND_MAX_TOOL_ROUNDS", "5"))
    start_time = time.time()
    message = ""
    reasoning = ""
    tool_calls_executed = []
    tokens_in = 0
    tokens_out = 0
    result = None

    for tool_round in range(max_tool_rounds + 1):
        try:
            result = _call_openrouter(
                context["system_prompt"], llm_messages,
                tools=context["tools"] if context["tools"] else None,
            )
        except http_requests.Timeout:
            result = None
            logger.warning("DnD inference timeout for %s in campaign %s",
                           agent_id, campaign_code)
            break
        except Exception as e:
            result = None
            logger.warning("DnD inference error for %s: %s", agent_id, e)
            break

        if not result or not result.get("choices"):
            break

        choice = result["choices"][0]
        msg = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")

        # Accumulate usage
        usage = result.get("usage", {})
        tokens_in += usage.get("prompt_tokens", 0)
        tokens_out += usage.get("completion_tokens", 0)
        cost = _estimate_cost_cents(usage)
        _add_spend(campaign_code, cost, redis_client)

        # Capture reasoning from any round
        if msg.get("reasoning"):
            reasoning += (msg["reasoning"] + "\n")

        # Capture content from any round — some models send content
        # alongside tool_calls; only reading it on the final "stop"
        # round loses the narration entirely.
        if msg.get("content"):
            message = msg["content"]

        # If the model is done (no more tool calls), break
        if finish_reason == "stop" or not msg.get("tool_calls"):
            break

        # Execute tool calls and feed results back
        llm_messages.append(msg)  # the assistant's tool_call message
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            fn_name = fn.get("name", "")
            try:
                fn_args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                fn_args = {}

            tc_result = _execute_tool_call(
                fn_name, fn_args, campaign_code, agent_id, redis_client)
            tool_calls_executed.append({
                "name": fn_name,
                "args": fn_args,
                "result": tc_result,
            })

            # Feed the tool result back to the model
            llm_messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": tc_result if isinstance(tc_result, str) else json.dumps(tc_result),
            })

        # Loop continues — model will see tool results and generate narration

    end_time = time.time()

    if not message:
        name = agent_config.get("name", agent_id)
        if agent_config.get("role") == "dm":
            message = f"The world holds its breath as {name} gathers their thoughts..."
        else:
            message = f"{name} pauses, considering their next move..."

    # Determine entry type
    if agent_config.get("role") == "dm":
        entry_type = "narration"
    else:
        entry_type = "action"

    # --- Langfuse trace ---
    _trace_generation(
        "dnd_agent_turn", result.get("model", DND_MODEL) if result else DND_MODEL,
        {"agent_id": agent_id, "role": agent_config.get("role"),
         "messages_count": len(llm_messages)},
        {"message_length": len(message), "tool_calls": len(tool_calls_executed)},
        metadata={
            "campaign_code": campaign_code,
            "round": current_round,
            "agent_name": agent_config.get("name", agent_id),
        },
        start_time=start_time, end_time=end_time,
    )

    # --- Build log entry ---
    log_entry = {
        "round": current_round,
        "agent_id": agent_id,
        "agent_name": agent_config.get("name", agent_id),
        "type": entry_type,
        "message": message,
        "tool_calls": tool_calls_executed,
        "reasoning": reasoning,
        "whispers_received": [
            {"from": w.get("from", "player"), "text": w.get("text", "")}
            for w in whispers
        ],
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "context_estimate": token_estimate,
        "prompt_context": {
            "system_prompt": context["system_prompt"],
            "messages": llm_messages,
            "tools": [t["function"]["name"] for t in context["tools"]]
                     if context["tools"] else [],
        },
        "ts": int(time.time()),
    }

    if compaction_entry:
        log_entry["compaction"] = {
            "rounds_compacted": compaction_entry.get("rounds_compacted", []),
            "facts_extracted": len(compaction_entry.get("facts_extracted", [])),
            "tokens_before": compaction_entry.get("tokens_before", 0),
            "tokens_after": compaction_entry.get("tokens_after", 0),
        }

    return log_entry
