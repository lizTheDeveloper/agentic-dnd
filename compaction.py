"""Context compaction: keeping long campaigns inside the context window.

Every turn replays the whole log as chat history, so the prompt grows
until it no longer fits.  Compaction trades old history for two cheaper
things:

  1. estimate_context_tokens()     count how full the window is (chars / 4)
  2. should_compact()              trigger when > 70% full
  3. extract_facts_from_history()  DM pulls durable facts out of the old
                                   rounds into long-term memory (memory.py)
  4. summarize_history()           DM writes a ~500-token "Previously on..."
  5. compact_campaign()            replaces the old rounds in the log with
                                   one compaction entry holding the summary

The last DND_COMPACTION_KEEP_ROUNDS rounds are always kept verbatim.
agent.py calls this automatically before each turn.
"""

import json
import logging
import math
import os
import time

import llm
import memory
from store import current_round, key
from tools import TOOL_SCHEMAS
from tracing import trace_generation

logger = logging.getLogger(__name__)

DND_MODEL_CONTEXT_WINDOW = int(os.environ.get("DND_MODEL_CONTEXT_WINDOW", "32000"))
DND_COMPACTION_KEEP_ROUNDS = int(os.environ.get("DND_COMPACTION_KEEP_ROUNDS", "3"))

EXTRACT_SYSTEM_PROMPT = (
    "You are the Dungeon Master reviewing your campaign notes. "
    "Extract ALL important facts from the following campaign history "
    "into structured facts. Call remember_fact() for EACH fact — "
    "plot points, NPC relationships, player decisions, location details, "
    "items found, world-state changes. Be thorough; anything not "
    "extracted will be lost when the history is summarized."
)

SUMMARIZE_SYSTEM_PROMPT = (
    "You are the Dungeon Master. Summarize the campaign so far in "
    "approximately 500 tokens for the party's records. Write in your "
    "narrative voice — this summary will be read as 'Previously on our "
    "adventure...' at the start of the next session. Include key events, "
    "important decisions the party made, and unresolved threads. Do NOT "
    "use bullet points — write flowing narrative prose."
)


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

    def _as_text(x):
        if isinstance(x, str):
            return x
        if isinstance(x, list):
            return json.dumps(x)
        return ""

    breakdown = {
        "system": _count(agent_config.get("system_prompt", "")),
        "tools": _count(_as_text(tools)),
        "history": _count(_as_text(history)),
        "facts": _count(facts if isinstance(facts, str) else json.dumps(facts or "")),
        "notes": _count(notes if isinstance(notes, str) else json.dumps(notes or "")),
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
# Phase 1: fact extraction (old history -> long-term memory)
# ---------------------------------------------------------------------------

def extract_facts_from_history(campaign_code, rounds_to_extract, redis_client):
    """Extract structured facts from old rounds via a DM inference call.

    Makes ONE call to the DM agent with remember_fact as the only tool.
    Parses tool calls from the response and executes each against Redis.

    Returns: list of extracted facts [{fact, category, round}]
    """
    if not rounds_to_extract:
        return []

    history_text = format_rounds_for_compaction(rounds_to_extract)
    messages = [{"role": "user", "content": history_text}]
    tools = [TOOL_SCHEMAS["remember_fact"]]

    start_time = time.time()
    extracted = []

    try:
        result = llm.call_openrouter(EXTRACT_SYSTEM_PROMPT, messages, tools=tools,
                                     max_tokens=llm.DND_MAX_TOKENS * 2)
        end_time = time.time()

        # Parse tool calls from response
        if result and result.get("choices"):
            msg = result["choices"][0].get("message", {})
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                if fn.get("name", "") != "remember_fact":
                    continue
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    args = {}
                fact_text = args.get("fact", "")
                category = args.get("category", "world_state")
                if fact_text:
                    # Execute against Redis (additive)
                    memory.remember_fact(campaign_code, "dm_compaction",
                                         fact_text, category, redis_client)
                    extracted.append({
                        "fact": fact_text,
                        "category": category,
                        "round": "compaction",
                    })

        llm.add_spend(campaign_code, result.get("usage", {}) if result else {}, redis_client)

        trace_generation(
            "compaction_extract", llm.DND_MODEL,
            {"system": EXTRACT_SYSTEM_PROMPT, "history_length": len(history_text)},
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
# Phase 2: summarization (old history -> one narrative recap)
# ---------------------------------------------------------------------------

def summarize_history(campaign_code, rounds_to_summarize, redis_client):
    """Summarize old rounds into a narrative recap (~500 tokens).

    Makes ONE inference call to the DM agent.
    Returns: summary text (string).
    """
    if not rounds_to_summarize:
        return ""

    history_text = format_rounds_for_compaction(rounds_to_summarize)
    messages = [{"role": "user", "content": history_text}]

    start_time = time.time()
    summary = ""

    try:
        result = llm.call_openrouter(SUMMARIZE_SYSTEM_PROMPT, messages, tools=None,
                                     max_tokens=700)
        end_time = time.time()

        if result and result.get("choices"):
            msg = result["choices"][0].get("message", {})
            summary = msg.get("content", "") or ""

        llm.add_spend(campaign_code, result.get("usage", {}) if result else {}, redis_client)

        trace_generation(
            "compaction_summarize", llm.DND_MODEL,
            {"system": SUMMARIZE_SYSTEM_PROMPT, "history_length": len(history_text)},
            {"summary_length": len(summary)},
            metadata={"campaign_code": campaign_code},
            start_time=start_time, end_time=end_time,
        )

    except Exception as e:
        logger.warning("History summarization failed: %s", e)
        summary = "(Summary unavailable — previous rounds were compacted.)"

    return summary


# ---------------------------------------------------------------------------
# The full cycle
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
    log_raw = redis_client.lrange(key(campaign_code, "log"), 0, -1) or []
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
    compaction_entry = {
        "type": "compaction",
        "round": current_round(campaign_code, redis_client),
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
    log_key = key(campaign_code, "log")
    redis_client.delete(log_key)
    for entry in new_log:
        redis_client.rpush(log_key, json.dumps(entry))

    # 8. Bump campaign state
    redis_client.incr(key(campaign_code, "_bump"))

    logger.info(
        "Compacted campaign %s: %d rounds -> summary (%d->%d est. tokens), "
        "%d facts extracted",
        campaign_code, len(rounds_to_compact),
        tokens_before, compaction_entry["tokens_after"],
        len(extracted_facts),
    )

    return compaction_entry


def format_rounds_for_compaction(entries):
    """Format log entries into readable text for the compaction LLM calls."""
    lines = []
    current = None
    for entry in entries:
        rnd = entry.get("round", "?")
        if rnd != current:
            current = rnd
            lines.append(f"\n--- Round {rnd} ---")

        agent_name = entry.get("agent_name", entry.get("agent_id", "Unknown"))
        msg = entry.get("message", "")
        entry_type = entry.get("type", "action")

        lines.append(f"[{agent_name}] ({entry_type}): {msg}")

        # Include tool call results for context
        for tc in entry.get("tool_calls", []):
            lines.append(f"  -> {tc.get('name', '?')}: {tc.get('result', '')}")

    return "\n".join(lines)
