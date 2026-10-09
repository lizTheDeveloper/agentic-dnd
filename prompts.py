"""What the model sees: system prompt, history, and the per-turn nudge.

The system prompt is assembled in layers, in this order:

  1. the student's character sheet (markdown, verbatim)
  2. DM only: "use lookup_rule" if a rulebook was uploaded   (RAG)
  3. DM only: the five-step turn procedure                   (memory habits)
  4. a short role instruction (DM vs. player)
  5. World Facts, then this agent's Private Notes            (memory, pushed)

History is the campaign log replayed from this agent's point of view: its
own turns are "assistant" messages, everyone else's are "user" messages
prefixed with the speaker's name.  A compaction entry stands in for all
the turns it replaced (see compaction.py).
"""

import json

import memory
import retrieval
from store import key
from tools import tool_defs

DM_TURN_PROCEDURE = (
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

DM_ROLE = (
    "\n\nYou are the Dungeon Master. You control the world, NPCs, and "
    "story — the players control their characters. Follow your turn "
    "procedure above on every turn."
)


def rulebook_instruction(name):
    return (
        f"\n\nYou have access to the [{name}] rulebook via "
        f"the lookup_rule tool. When a player attempts an action that "
        f"involves game mechanics (combat, saves, magic, inventory), "
        f"ALWAYS call lookup_rule first rather than inventing rules. "
        f"Quote the rule in your narration when relevant."
    )


def player_role(name):
    return (
        f"\n\nYou are playing as {name}. "
        f"Stay in character. Be bold and decisive — declare specific "
        f"actions, speak dialogue in first person. Use roll_dice() when "
        f"you attempt something risky or uncertain. Use take_note() for "
        f"things you want to remember."
    )


# The last user message of every turn: "it's your turn, here's what to do".
DM_OPENING_NUDGE = (
    "Begin the campaign. Use set_scene to establish the "
    "opening location, then narrate the scene the "
    "adventurers find themselves in. End by describing "
    "what the characters see and asking what they do."
)
DM_TURN_NUDGE = (
    "It is your turn. Follow your turn procedure: "
    "recall_facts → resolve declared player actions → "
    "narrate the world → remember_fact for anything "
    "important → end with a situation that invites the "
    "players to act."
)
PLAYER_TURN_NUDGE = (
    "It is your turn. Declare what your character does — be "
    "specific and decisive. Speak dialogue in first person. "
    "If you attempt something risky, use roll_dice."
)


def build_agent_context(agent_config, campaign_code, redis_client):
    """Assemble system prompt, messages, facts, notes, and tools for an agent.

    Returns: {system_prompt, messages, facts_text, notes_text, tools, tools_json}
    """
    agent_id = agent_config["id"]
    role = agent_config.get("role", "player")
    is_dm = role == "dm"

    # --- System prompt layers 1-4 ---
    full_system = agent_config.get("system_prompt", "")
    if is_dm:
        name = retrieval.rulebook_name(campaign_code, redis_client)
        if name:
            full_system += rulebook_instruction(name)
        full_system += DM_TURN_PROCEDURE + DM_ROLE
    else:
        full_system += player_role(agent_config.get("name", "a character"))

    # --- Conversation history from the log ---
    log_raw = redis_client.lrange(key(campaign_code, "log"), 0, -1) or []
    messages = []
    for entry_raw in log_raw:
        entry = json.loads(entry_raw)

        # Handle compaction entries — include summary as context
        if entry.get("type") == "compaction":
            summary = entry.get("summary", "")
            if summary:
                messages.append({
                    "role": "assistant" if is_dm else "user",
                    "content": f"[Campaign Summary — Previously:]\n{summary}",
                })
            continue

        # Regular turn entries
        entry_agent = entry.get("agent_id", "")
        entry_message = entry.get("message", "")

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

    # --- Layer 5: memory, pushed into the system prompt ---
    facts_text = memory.facts_for_prompt(campaign_code, redis_client)
    notes_text = memory.notes_for_prompt(campaign_code, agent_id, redis_client)
    if facts_text:
        full_system += f"\n\n{facts_text}"
    if notes_text:
        full_system += f"\n\n{notes_text}"

    tools = tool_defs(agent_config.get("tools", []))

    return {
        "system_prompt": full_system,
        "messages": messages,
        "facts_text": facts_text,
        "notes_text": notes_text,
        "tools": tools,
        "tools_json": json.dumps(tools),
    }


def whisper_message(whispers):
    """Pending whispers become one private user message."""
    whisper_text = "\n".join(
        f"[Whisper from {w.get('from', 'unknown')}]: {w.get('text', '')}"
        for w in whispers
    )
    return {
        "role": "user",
        "content": f"[Private whispers for you — do not reveal these directly]\n{whisper_text}",
    }


def turn_nudge(agent_config, current_round, has_history):
    """The final user message that tells the agent to act."""
    if agent_config.get("role") == "dm":
        if current_round == 1 and not has_history:
            return {"role": "user", "content": DM_OPENING_NUDGE}
        return {"role": "user", "content": DM_TURN_NUDGE}
    return {"role": "user", "content": PLAYER_TURN_NUDGE}
