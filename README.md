# Agentic D&D

A multi-agent teaching tool from [The Multiverse School](https://themultiverse.school). Students drop in markdown character sheets (one DM, 3–5 players) and watch AI agents play a turn-based campaign with tool use, whispers, world facts, private notes, and retrieval over an uploaded rulebook.

Live version: https://themultiverse.school/tools/agentic-dnd

This repo is the readable version, split into one module per concept so it can be read on screen in class. On the school's site the same code is kept as three single files (a Flask blueprint, an inference module and one HTML page). The behavior, prompts, tool schemas, Redis keys and URLs are the same; the differences are listed under [Differences from the live site](#differences-from-the-live-site).

## Running it

You need Python 3.10+, [uv](https://docs.astral.sh/uv/), Redis, and an [OpenRouter](https://openrouter.ai) API key.

```sh
uv venv && uv pip install -r requirements.txt
cp .env.example .env            # then put your OPENROUTER_API_KEY in it
set -a && source .env && set +a

redis-server                    # in another terminal
.venv/bin/python app.py         # or: source .venv/bin/activate && python app.py
```

Open http://localhost:8000/tools/agentic-dnd. Without `OPENROUTER_API_KEY` the app still runs, but each agent only "pauses to think" instead of taking a real turn.

Environment variables (all in `.env.example`):

- `OPENROUTER_API_KEY` (required for real turns)
- `DND_INFERENCE_MODEL` (default `openrouter/auto`), `DND_INFERENCE_MAX_TOKENS`, `DND_INFERENCE_TIMEOUT`, `DND_MAX_TOOL_ROUNDS`
- `DND_MODEL_CONTEXT_WINDOW` (default 32000) and `DND_COMPACTION_KEEP_ROUNDS` (default 3) control compaction
- `REDIS_URL` (default `redis://localhost:6379/0`), `FLASK_SECRET_KEY`, `PORT` (default 8000)
- `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` (optional; tracing is off when unset)

## Files

The backend is a set of flat Python modules with no package and no framework beyond Flask. Each one can be read from top to bottom.

| File | What it is |
| --- | --- |
| `app.py` | Creates the Flask app, registers the routes, and runs it with `python app.py` |
| `routes.py` | HTTP routes: create, state, advance turn, whisper, export, status |
| `auth.py` | Anonymous session id (who created a campaign) and rate limiting |
| `store.py` | The Redis client, `dnd:{code}:*` key helpers, and the key layout |
| `campaign.py` | Turns character sheets into agents, saves a new campaign, and reads state back |
| `agent.py` | One agent's turn: the agent loop (context, then compaction, then LLM, then tools, repeated) |
| `prompts.py` | System prompt layers, history replay, and the "it's your turn" nudges |
| `tools.py` | Tool JSON schemas and `execute_tool_call()` |
| `memory.py` | Long-term memory: world facts (shared) and private notes (per agent) |
| `retrieval.py` | RAG: keyword and title scoring over rulebook chunks |
| `compaction.py` | Token estimates, the 70% trigger, fact extraction, summarization, and log rewrite |
| `llm.py` | The OpenRouter call and spend tracking |
| `tracing.py` | Optional Langfuse tracing |
| `static/index.html` | Page markup; loads the CSS and the scripts in order |
| `static/css/agentic-dnd.css` | All styles |
| `static/js/state.js` | Shared constants, page state, and DOM helpers (loaded first) |
| `static/js/chunker.js` | The four chunking strategies, written as pure functions |
| `static/js/chunker-ui.js` | Rulebook panel: pick a strategy, preview chunks, see size stats |
| `static/js/setup.js` | Setup screen: draft autosave, character slots, Launch |
| `static/js/roster.js` | Top bar, agent roster, whispers |
| `static/js/turn-log.js` | Campaign log, with one card per turn |
| `static/js/turn-detail.js` | "How this turn worked": reasoning, prompt, tools, tokens, JSON |
| `static/js/rag-flow.js` | Pipeline view of a `lookup_rule` call (query, chunks, matches, result) |
| `static/js/context-meter.js` | Context fill bar, its breakdown, and the compaction card |
| `static/js/memory-panel.js` | World-facts panel with category filter |
| `static/js/architecture.js` | Campaign stats and the 12-factor scorecard |
| `static/js/game.js` | Enter campaign, poll `/state`, render, and the right-hand controls |
| `static/js/export.js` | JSON and Markdown export |
| `static/js/app.js` | Panel toggles and boot (loaded last) |
| `static/examples/cairn-srd-example.txt` | Cairn SRD for the "Try Cairn 2e" button (CC-BY-SA 4.0, Yochai Gal) |

The frontend has no bundler and no build step. Each file is a plain `<script>` tag, and the scripts share top-level `const`, `let` and function declarations. Load order matters: `state.js` comes first and `app.js` comes last.

## Where RAG and memory happen

**RAG: chunk, store, retrieve**

1. Chunking happens in the browser. `chunkHeader`, `chunkParagraph`, `chunkFixed` and `chunkSemantic` are in `static/js/chunker.js`. The chunker panel lets you compare them on the same text.
2. The chunks are stored by `save_new_campaign()` in `campaign.py`, which writes them to the `dnd:{code}:rulebook` hash.
3. Retrieval is `lookup_rule()` in `retrieval.py`. It uses keyword and title scoring and returns the top 3 chunks, with no embeddings.
4. The DM is told to use it by `rulebook_instruction()` in `prompts.py`, and calls it through the `lookup_rule` tool in `tools.py`.
5. You can watch it work in `buildRagFlow()` in `static/js/rag-flow.js`.

**Memory: facts, notes, compaction**

- Writes go through the `remember_fact` and `take_note` tools. They are defined in `tools.py` and implemented in `memory.py`, which stores them in `dnd:{code}:facts` and `dnd:{code}:notes:{agent}`.
- Memory is read back in two ways:
  - Pushed: `facts_for_prompt()` and `notes_for_prompt()` are appended to every system prompt in `build_agent_context()` in `prompts.py`.
  - Pulled: the agent can call the `recall_facts` and `read_notes` tools.
- The habit of using memory comes from the DM's five-step turn procedure, `DM_TURN_PROCEDURE` in `prompts.py`.
- Context compaction lives in `compaction.py`, and `run_agent_turn()` in `agent.py` runs it before each turn. When the estimated context passes 70% of `DND_MODEL_CONTEXT_WINDOW`:
  1. The DM extracts facts from the old rounds into memory.
  2. The DM summarizes those rounds.
  3. The old rounds in the log are replaced by a single compaction entry. The last `DND_COMPACTION_KEEP_ROUNDS` rounds stay verbatim.
- You can watch it in `static/js/context-meter.js` and `static/js/memory-panel.js`.

## Differences from the live site

- **Auth.** The live site requires a school login. Here, each browser gets an anonymous id in its signed session cookie (`auth.py`), and that id decides who created a campaign and so who may advance it, whisper in it, or pause it. Rate limits are unchanged (10 campaigns an hour, 30 whispers a minute), but they are keyed on the client IP instead of the user id.
- **Redis.** The client comes from `REDIS_URL` in `store.py` instead of the host app's shared client.
- **Static files.** The page loads `/static/css/...` and `/static/js/...` instead of inlining them. The Cairn example is served from `/static/examples/`.
