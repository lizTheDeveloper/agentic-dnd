"""Rulebook retrieval — the RAG part.

Students paste a rulebook into the browser, pick a chunking strategy
(static/js/chunker.js), and the chunks are stored in dnd:{code}:rulebook
when the campaign is created.  When the DM calls the lookup_rule tool,
we score every chunk against the query and return the top 3.

There are no embeddings here on purpose: scoring is keyword + title
matching, so you can read exactly why a chunk was retrieved.  Swapping
in vector search would mean replacing the scoring loop in lookup_rule().
"""

import json

from store import key

TOP_K = 3
MAX_CHUNK_CHARS = 600  # truncate long chunks to stay within token budget


def lookup_rule(query, campaign_code, redis_client):
    """Search the loaded rulebook for relevant chunks.

    Performs simple keyword + title matching against student-uploaded chunks
    stored in Redis.  Returns top 3 matches with content so the DM can
    quote the rule in its narration.
    """
    if not query:
        return "[Rule lookup: empty query]"

    # Load chunks from Redis hash
    raw_chunks = redis_client.hgetall(key(campaign_code, "rulebook")) or {}
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
    top = scored[:TOP_K]

    if not top:
        return (f"[Rule lookup: {query}] — No matching rules found in the "
                f"rulebook.  The Warden adjudicates based on their judgment.")

    parts = [f"[Rulebook lookup: \"{query}\"]"]
    for score, cid, chunk in top:
        title = chunk.get("title", cid)
        content = chunk.get("content", "")
        if len(content) > MAX_CHUNK_CHARS:
            content = content[:MAX_CHUNK_CHARS - 3] + "..."
        parts.append(f"\n--- {title} (relevance: {score}) ---\n{content}")

    return "\n".join(parts)


def rulebook_name(campaign_code, redis_client):
    """Return the rulebook name from campaign meta, or None."""
    try:
        return redis_client.hget(key(campaign_code, "meta"), "rulebook_name")
    except Exception:
        return None
