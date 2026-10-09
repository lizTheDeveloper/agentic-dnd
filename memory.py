"""Long-term memory: world facts and private notes.

Two stores, both plain Redis hashes of JSON:

  dnd:{code}:facts         shared world facts.  Only the DM can write them
                           (remember_fact) but every agent sees them.
  dnd:{code}:notes:{aid}   one agent's private notebook (take_note).
                           Nobody else ever sees it.

Memory reaches the model two ways:
  1. Pushed: facts_for_prompt() / notes_for_prompt() are appended to the
     system prompt on every turn (see prompts.py).
  2. Pulled: the agent calls recall_facts / read_notes as tools.

Compaction (compaction.py) also writes here: before old turns are
summarized away, the DM extracts their facts with remember_fact.
"""

import json
import time
import uuid

from store import current_round, key


# ---------------------------------------------------------------------------
# World facts (shared)
# ---------------------------------------------------------------------------

def remember_fact(campaign_code, agent_id, fact_text, category, redis_client):
    fact_id = str(uuid.uuid4())[:8]
    fact_data = {
        "fact": fact_text,
        "category": category,
        "remembered_by": agent_id,
        "round": current_round(campaign_code, redis_client),
        "ts": int(time.time()),
    }
    redis_client.hset(key(campaign_code, "facts"), fact_id, json.dumps(fact_data))
    return f"Fact remembered: {fact_text}"


def recall_facts(campaign_code, category, redis_client):
    facts_raw = redis_client.hgetall(key(campaign_code, "facts")) or {}
    facts = []
    for fid, v in facts_raw.items():
        f = json.loads(v)
        if category and f.get("category") != category:
            continue
        facts.append(f"[{f.get('category', '?')}] {f.get('fact', '')}")
    if not facts:
        return "No facts recorded yet." if not category else f"No facts in category '{category}'."
    return "\n".join(facts)


def facts_for_prompt(campaign_code, redis_client):
    """All world facts as a block for the system prompt ("" if none)."""
    facts_raw = redis_client.hgetall(key(campaign_code, "facts")) or {}
    facts_lines = []
    for fid, v in facts_raw.items():
        f = json.loads(v)
        facts_lines.append(f"- [{f.get('category', '?')}] {f.get('fact', '')}")
    if not facts_lines:
        return ""
    return "World Facts:\n" + "\n".join(facts_lines)


# ---------------------------------------------------------------------------
# Private notes (per agent)
# ---------------------------------------------------------------------------

def take_note(campaign_code, agent_id, note_text, tag, redis_client):
    note_id = str(uuid.uuid4())[:8]
    note_data = {"note": note_text, "tag": tag, "ts": int(time.time())}
    redis_client.hset(
        key(campaign_code, f"notes:{agent_id}"), note_id, json.dumps(note_data))
    return f"Note taken ({tag}): {note_text[:60]}..."


def read_notes(campaign_code, agent_id, tag, redis_client):
    notes_raw = redis_client.hgetall(key(campaign_code, f"notes:{agent_id}")) or {}
    notes = []
    for nid, v in notes_raw.items():
        n = json.loads(v)
        if tag and n.get("tag") != tag:
            continue
        notes.append(f"[{n.get('tag', '?')}] {n.get('note', '')}")
    if not notes:
        return "No notes yet." if not tag else f"No notes with tag '{tag}'."
    return "\n".join(notes)


def notes_for_prompt(campaign_code, agent_id, redis_client):
    """This agent's private notes as a block for the system prompt ("" if none)."""
    notes_raw = redis_client.hgetall(key(campaign_code, f"notes:{agent_id}")) or {}
    notes_lines = []
    for nid, v in notes_raw.items():
        n = json.loads(v)
        notes_lines.append(f"- [{n.get('tag', '?')}] {n.get('note', '')}")
    if not notes_lines:
        return ""
    return "Your Private Notes:\n" + "\n".join(notes_lines)
