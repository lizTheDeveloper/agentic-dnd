"""Optional Langfuse tracing.

Each LLM call is recorded as one trace + one generation through
Langfuse's REST ingestion API.  With LANGFUSE_PUBLIC_KEY /
LANGFUSE_SECRET_KEY unset this is a silent no-op, and it never raises:
tracing must not break gameplay.
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone

import requests as http_requests

logger = logging.getLogger(__name__)

LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST",
                               "https://langfuse.multiversestudios.xyz")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")

_NOOP_LOGGED = False


def _iso(ts):
    return (datetime.fromtimestamp(ts, tz=timezone.utc)
            .isoformat().replace("+00:00", "Z"))


def trace_generation(name, model, input_data, output_data, *,
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
