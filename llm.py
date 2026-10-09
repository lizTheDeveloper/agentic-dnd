"""The LLM call: one OpenRouter chat completion, plus spend tracking.

Everything that talks to a model (agent turns and both compaction steps)
goes through call_openrouter().  It speaks the OpenAI chat-completions
format, so the tool schemas in tools.py are OpenAI function-calling JSON.
"""

import logging
import os

import requests as http_requests

from store import spend_key

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
DND_MODEL = os.environ.get("DND_INFERENCE_MODEL", "openrouter/auto")
DND_MAX_TOKENS = int(os.environ.get("DND_INFERENCE_MAX_TOKENS", "4096"))
DND_TIMEOUT = int(os.environ.get("DND_INFERENCE_TIMEOUT", "60"))

# Re-exported so callers can catch timeouts without importing requests.
Timeout = http_requests.Timeout


def call_openrouter(system, messages, *, tools=None, max_tokens=None):
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
# Spend tracking
# ---------------------------------------------------------------------------

def estimate_cost_cents(usage):
    """Rough cost estimate.  OpenRouter returns usage but we use flat rates."""
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    # Generous flat rate: $1/M input, $3/M output
    input_cost = (prompt_tokens / 1_000_000) * 1.0
    output_cost = (completion_tokens / 1_000_000) * 3.0
    return round((input_cost + output_cost) * 100, 4)


def add_spend(campaign_code, usage, redis_client):
    """Accumulate inference spend for a campaign."""
    cost_cents = estimate_cost_cents(usage)
    try:
        redis_client.incrbyfloat(spend_key(campaign_code), cost_cents)
    except Exception:
        pass
