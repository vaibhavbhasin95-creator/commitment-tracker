# rag: ask questions about your meetings

Search over a person's saved meetings, and answers **built only from those meetings, with the source of every claim**. If the meetings don't cover a question, it says so instead of guessing. This is the "R" and "G" of retrieval-augmented generation: the app finds the relevant excerpts first, then asks an AI engine to answer from those excerpts alone.

An optional, self-contained feature, like `ai_extraction.py`. `RAG_ENABLED=0` removes the page, the routes, the indexing and the extra delete steps; the rest of the app behaves exactly as before.

## What a user sees

Sidebar → **Ask your meetings** (`/ask`). Type a question, get an answer with `[1]`, `[2]` markers that link to the excerpts they came from (meeting, date, speakers, text). A question the meetings don't cover gets "I could not find this in your meetings", and the AI is never called. A cloud AI engine asks for a one-time OK before any meeting text is sent to it.

## How it works

```
INDEX (automatic, in the background)            ASK (when someone asks)
meeting saved or edited                         question
  → job: index_meeting                            → embed the question (local model)
  → chunk by speaker turns        chunking.py     → vector search + keyword search   retriever.py
  → embed every chunk             llm.py            → merge the two rankings
  → store in SQLite               indexer.py      → keep chunks that look relevant   answer.py
                                                  → no relevant chunk? "could not find" (no AI call)
                                                  → numbered excerpts + question → AI engine   llm.py
                                                  → answer + the excerpts it cites
```

**Embeddings** are made locally by `sentence-transformers` (`all-MiniLM-L6-v2`, 384 numbers per chunk). Nothing is sent anywhere to index. The first use downloads the model (about 90 MB) from Hugging Face. **Chat** goes through LiteLLM to whichever AI engine the account saved on the AI extraction page.

## Files

| File | What it does |
|---|---|
| `chunking.py` | Splits a transcript into chunks of whole speaker turns (target 800 characters, cap 1,100, one turn of overlap). Pure function, no database |
| `indexer.py` | `index_meeting` builds a meeting's chunks, vectors and keyword rows in one transaction; `delete_index` removes them; `sweep_dirty`, `prepare` keep the index fresh |
| `retriever.py` | `search`: cosine similarity over stored vectors plus SQLite FTS5 keyword search, merged by reciprocal rank fusion |
| `answer.py` | `answer_question`: relevance check, prompt, citations, query log |
| `llm.py` | `chat` (LiteLLM, readable errors, reply-length limit) and `embed` (sentence-transformers) |
| `settings.py` | `load_engine` finds the AI engine to use; `grant_rag_consent` records the one-time cloud consent |
| `schema.py` | The tables and the triggers (`ensure_schema`, `drop_triggers`) |
| `routes.py` | The `/ask` page and `/api/rag/*` routes |
| `backfill.py` | One-off command to index meetings that already exist |
| `errors.py` | `RagError`, `RagConsentRequired`, `MeetingNotFound` |

## Where the data lives

Everything is in the app's own database (`commitment_tracker.db`), next to meetings and commitments. There is no separate vector database.

| Table | Holds |
|---|---|
| `rag_chunks` | One row per chunk: text, meeting, owner, speakers, turn range. Transcript chunks and one chunk per commitment (task, owner, due date, status) |
| `rag_embeddings` | The 384-number vector of each chunk as float32 bytes, with the model name and size |
| `rag_fts` | SQLite FTS5 keyword index over the chunk text (its rowid is the chunk id) |
| `rag_queries` | Log of questions asked: question, answer, cited chunk ids, engine, time (also unanswered ones) |
| `rag_dirty` | Meetings whose index is out of date |
| `rag_settings` | The one-time consent to send meetings to a cloud AI engine |

Five triggers on `meetings` and `commitments` only ever write to `rag_dirty`. They fire when a transcript, title, date, task text, owner, due date, status or source quote changes, and not for other columns. The vectors are searched in memory with numpy, which is plenty for thousands of chunks. Move to `sqlite-vec` (or `pgvector` on Postgres) only if a single account grows to tens of thousands of chunks and questions get slow.

## Keeping the index fresh

Automatic; nobody needs to run anything.

1. A trigger marks the meeting in `rag_dirty` whenever something searchable about it changes, from any route, webhook or Sheet edit.
2. Every 30 seconds a sweeper (`search index refresh`, a periodic task in `jobs.py`) queues one `index_meeting` job for each meeting that has been quiet for 20 seconds. A burst of edits becomes one job, and a job already waiting isn't queued twice.
3. On the first sweep after the app starts, meetings that were never indexed are marked too, so a database from before RAG, or one where it was switched off, catches up by itself.
4. Uploading a meeting queues indexing right after extraction (even if extraction failed, since the transcript is searchable regardless).
5. Deleting or clearing meetings, or clearing tasks, removes the matching chunks, vectors and keyword rows in the same transaction.

An edit shows up in answers within about 50 seconds. Re-run `python src/rag/backfill.py` only after changing chunk sizes or the embedding model (old chunks keep the old settings).

## Which AI engine answers

`load_engine` reads the engine saved on the **AI extraction** page, so there is nothing separate to set up. It does not matter which engine extraction is using, **including Built-in**: if an AI engine was saved earlier, RAG uses it. Order: the active engine, then OpenAI-compatible, then Anthropic.

- **Local endpoint** (`localhost`, e.g. GPT4All or Ollama): no consent needed, nothing leaves the machine.
- **Cloud endpoint:** the first question shows a one-time confirmation ("Send your meetings to …?"), stored in `rag_settings`. If that engine is also the active extraction engine, its existing consent is used.
- **No engine saved:** a clear message says to add one on the AI extraction page. A question with no relevant meetings needs no engine at all.

GPT4All notes: turn on its local API server (Settings → Application → Enable Local Server in version 3.x; menu names can differ, default `http://localhost:4891/v1`), keep the app open, use the model name exactly as `/v1/models` lists it, and raise the model's context length (4096 or more) if answers look truncated.

## Privacy

- Every query carries the account's `owner_email` in the SQL `WHERE` clause (vector search, keyword search and the final chunk load), never applied afterwards. Tests cover one account never reaching another's meetings through search, answers, re-indexing or deleting.
- Transcript text can't change the AI's instructions: it only ever appears inside the numbered `<context>` block, and a transcript can't close that block early.
- Questions and answers are stored in `rag_queries`, scoped to the account. They are not written to application logs.
- A cloud engine receives the question and the matching excerpts only after the one-time consent.

## Settings you can tune

These are starting points, chosen from a small sample. Phase 2 should calibrate them on a gold set of real questions.

| Setting | Where | Value |
|---|---|---|
| `RAG_ENABLED` | environment | `1` (on); `0` turns the whole feature off |
| Chunk size | `chunking.py` `TARGET_CHARS` / `MAX_CHARS` | 800 / 1,100 characters (the embedding model reads about 1,300) |
| Relevance, by meaning | `answer.py` `MIN_VECTOR_SCORE` | 0.30 cosine similarity |
| Relevance, by words | `answer.py` `MIN_KEYWORD_COVERAGE` | at least half of the question's words in the chunk |
| Excerpts per answer | `search(k=6)`, `MAX_CONTEXT_CHARS` | 6, within 6,000 characters |
| Candidates per search | `retriever.py` `POOL`, `RRF_K` | 20, 60 |
| Quiet period before re-indexing | `indexer.py` `QUIET_SECONDS` | 20 seconds |
| Longest reply | `llm.py` `DEFAULT_MAX_TOKENS` | 600 tokens (a server without a limit may stop after about 20) |
| AI call timeout | `llm.py` `CHAT_TIMEOUT_SECONDS` | 120 seconds |
| Longest question | `routes.py` `MAX_QUESTION_CHARS` | 1,000 characters |

## Routes

All need a signed-in session; POSTs need the `X-CT-Request: 1` header.

| Route | Purpose |
|---|---|
| `GET /ask` | The Ask page (signed-out visitors go to sign-in) |
| `GET /api/rag/status` | `{"enabled": true}`; the sidebar uses it to show the link |
| `POST /api/rag/ask` | `{"question": "..."}` → `answer`, `answered`, `citations`, `context`, `engine`, `query_id`. 400 bad question · 409 `consent_required` · 502 AI problem |
| `POST /api/rag/consent` | Records the one-time consent when a cloud engine is waiting for it |
| `POST /api/rag/reindex` | `{"meeting_id": N}` → rebuilds that meeting's index now (your own meetings only) |

## Running and checking it

```bash
python src/rag/backfill.py                        # index every existing meeting (safe to repeat)
python src/rag/backfill.py --email you@example.com
python src/tests/run_all.py rag                   # the RAG test suites (test_rag_*.py)
```

The tests use temporary databases and stand-ins for the embedding model and the AI: nothing is downloaded and nothing leaves the machine.

## How it connects to the app

`app.py` has one block marked `[rag]` (import, the `index_meeting` job, the sweeper, route registration) plus a few lines tagged `# [rag]`: queueing indexing after extraction, and purging the index when meetings or tasks are deleted. `static/app.js` adds the sidebar link. To remove the feature, delete those and call `rag.schema.drop_triggers(db)`; the tables can stay.

## Known limits

- **Quality depends on the AI engine.** A small local model can overstate what an excerpt says. The citations are there so a reader can check.
- **Common words match too much.** Keyword search ORs the question's words, so a generic word like "data" can pull in weak matches. The relevance check limits the damage; tuning it is a Phase 2 task.
- **Answers are one request.** `/api/rag/ask` waits for the AI (up to 120 seconds), and the first question after a start is slow while the models load.
- **One process.** Jobs and the sweeper run inside the app process, like the rest of the app.
- **Changing chunk sizes or the embedding model** needs a backfill. Vectors from a different model are ignored by vector search (keyword search still works) until re-indexed.
- **Not built yet:** an agent that decides what to look up (tool calls such as listing overdue tasks), a re-index button on the page, and a measured evaluation set.
