# Agentic D&D

A multi-agent teaching tool from [The Multiverse School](https://themultiverse.school). Students drop in markdown character sheets (one DM, 3–5 players) and watch AI agents play a turn-based campaign with tool use, whispers, world facts, private notes, and retrieval over an uploaded rulebook.

Live version: https://themultiverse.school/tools/agentic-dnd

This is a direct copy of the files from the school's app, not a standalone package. It expects to be mounted as a Flask blueprint inside a larger app.

## Files

- `agentic_dnd.py`: Flask blueprint with campaign create, state, advance turn, whisper and export routes. Campaign state lives in Redis.
- `dnd_inference.py`: the agent loop. It builds prompts and tools, retrieves rulebook chunks, compacts memory, and calls the model through OpenRouter.
- `static/tools/agentic-dnd.html`: the single-page frontend, including the rulebook chunker (header, paragraph, fixed and semantic) and the memory panel.

## Where to look for RAG and memory

- Rulebook retrieval: `dnd_inference.py`, the keyword and title matching over uploaded chunks.
- Long-term memory: the world-fact and private-note tools in `dnd_inference.py`, stored under `dnd:{code}:facts` and `dnd:{code}:notes:*`.
- Context compaction: `DND_COMPACTION_KEEP_ROUNDS` in `dnd_inference.py`.
- Chunking strategies: `chunkHeader`, `chunkParagraph`, `chunkFixed` and `chunkSemantic` in the HTML.

## Running it

You need Flask, `requests`, a Redis client exposed as `server.redis_client`, and these environment variables:

- `OPENROUTER_API_KEY` (required)
- `DND_INFERENCE_MODEL` (optional)
- `LANGFUSE_*` (optional; tracing is off when unset)
