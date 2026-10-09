"""Tools the agents can call: JSON schemas, and what each one does.

The schemas are OpenAI function-calling format; the model sees them and
decides which to call.  execute_tool_call() is the other half: given the
name and arguments the model chose, do the thing and return a string the
model reads back as the tool result.

Which agent gets which tools is decided in campaign.py (DM_TOOLS /
PLAYER_TOOLS).  The memory tools are thin wrappers over memory.py, and
lookup_rule is the RAG entry point in retrieval.py.
"""

import json
import random
import time

import memory
import retrieval
from store import key

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


def tool_defs(tool_names):
    """Return OpenAI-format tool definitions for the given tool names."""
    return [TOOL_SCHEMAS[t] for t in tool_names if t in TOOL_SCHEMAS]


def execute_tool_call(tool_name, args, campaign_code, agent_id, redis_client):
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
        redis_client.hset(key(campaign_code, "npcs"), npc_id, json.dumps(npc_data))
        return f"NPC '{npc_data['name']}' created (id: {npc_id})."

    elif tool_name == "lookup_rule":
        return retrieval.lookup_rule(args.get("query", ""), campaign_code, redis_client)

    elif tool_name == "view_npc_sheet":
        npc_id = args.get("npc_id", "")
        raw = redis_client.hget(key(campaign_code, "npcs"), npc_id)
        if raw:
            npc = json.loads(raw)
            return json.dumps(npc, indent=2)
        return f"NPC '{npc_id}' not found."

    elif tool_name == "set_scene":
        # The scene text itself lands in the log via the tool-call args.
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

    # ---- Memory tools (memory.py) ----
    elif tool_name == "remember_fact":
        return memory.remember_fact(
            campaign_code, agent_id, args.get("fact", ""),
            args.get("category", "world_state"), redis_client)

    elif tool_name == "recall_facts":
        return memory.recall_facts(campaign_code, args.get("category"), redis_client)

    elif tool_name == "take_note":
        return memory.take_note(
            campaign_code, agent_id, args.get("note", ""),
            args.get("tag", "general"), redis_client)

    elif tool_name == "read_notes":
        return memory.read_notes(campaign_code, agent_id, args.get("tag"), redis_client)

    return f"Unknown tool: {tool_name}"
