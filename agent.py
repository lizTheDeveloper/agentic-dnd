"""One agent's turn: the agent loop.

  1. Build the context (prompts.py) and estimate its size.
  2. If it's over 70% of the window, compact the campaign and rebuild.
  3. Append whispers and the "it's your turn" nudge.
  4. Call the model.  If it asks for tools, run them (tools.py), feed the
     results back, and call again -- up to DND_MAX_TOOL_ROUNDS times.
  5. Return a log entry: the narration plus everything the UI needs to
     show how the turn worked (tool calls, reasoning, tokens, prompt).
"""

import json
import logging
import os
import time

import llm
from compaction import compact_campaign, estimate_context_tokens, should_compact
from prompts import build_agent_context, turn_nudge, whisper_message
from store import current_round as get_current_round
from tools import execute_tool_call
from tracing import trace_generation

logger = logging.getLogger(__name__)


def _measure(agent_config, context):
    return estimate_context_tokens(
        agent_config,
        context["messages"],
        context["facts_text"],
        context["notes_text"],
        context["tools"],
    )


def run_agent_turn(campaign_code, agent_id, agent_config, whispers, redis_client):
    """Run one agent's turn: build context, call LLM, execute tools.

    Checks for compaction before building the final context.

    Returns: log_entry dict.
    """
    whispers = whispers or []

    # --- Compaction check (before building context) ---
    context = build_agent_context(agent_config, campaign_code, redis_client)
    token_estimate = _measure(agent_config, context)

    compaction_entry = None
    if should_compact(token_estimate):
        compaction_entry = compact_campaign(campaign_code, redis_client)
        if compaction_entry:
            # Rebuild context after compaction
            context = build_agent_context(agent_config, campaign_code, redis_client)
            token_estimate = _measure(agent_config, context)

    # --- Build messages for the LLM ---
    llm_messages = list(context["messages"])

    # Inject whispers as a private aside
    if whispers:
        llm_messages.append(whisper_message(whispers))

    # Prompt the agent to act
    current_round = get_current_round(campaign_code, redis_client)
    llm_messages.append(turn_nudge(agent_config, current_round,
                                   has_history=bool(context["messages"])))

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
            result = llm.call_openrouter(
                context["system_prompt"], llm_messages,
                tools=context["tools"] if context["tools"] else None,
            )
        except llm.Timeout:
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
        llm.add_spend(campaign_code, usage, redis_client)

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

            tc_result = execute_tool_call(
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

    is_dm = agent_config.get("role") == "dm"
    if not message:
        name = agent_config.get("name", agent_id)
        if is_dm:
            message = f"The world holds its breath as {name} gathers their thoughts..."
        else:
            message = f"{name} pauses, considering their next move..."

    # --- Langfuse trace ---
    trace_generation(
        "dnd_agent_turn", result.get("model", llm.DND_MODEL) if result else llm.DND_MODEL,
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
        "type": "narration" if is_dm else "action",
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
        # "What the agent saw" in the UI: the exact prompt for this turn
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
