# StrategyHub — Improvement Tasks

Work through these in order. Each task is self-contained; complete, verify, and commit before moving to the next. Repo structure: FastAPI backend in `backend/`, single-page frontend in `frontend/index.html`, DB schema in `supabase_setup.sql`, Supabase (pgvector) + OpenAI.

---

## Task 1 — Fix schema drift (CRITICAL)

**Problem:** Code writes columns that don't exist in `supabase_setup.sql`, so a fresh install crashes.
- `backend/services/scraper.py` (`scrape_source`) updates `sources.sitemap_url`
- `backend/routers/insights.py` (`summarise_source`) updates `sources.summary` and `sources.summary_generated_at`

**Do:**
1. Add to `supabase_setup.sql` on the `sources` table: `sitemap_url text`, `summary text`, `summary_generated_at timestamptz`.
2. Also create a separate idempotent migration snippet (`alter table ... add column if not exists ...`) at the bottom of the file (or a new `migrations.sql`) so existing databases can be upgraded.

**Verify:** Running the full SQL file on a fresh Supabase project succeeds; grep the codebase for every `.update({...})` on `sources` and confirm each column exists in the schema.

---

## Task 2 — Add scrape sessions table & fix change detection (CRITICAL)

**Problem:** `_store_content_chunks` (in `backend/services/scraper.py`) skips chunks whose `content_hash` already exists, so rescraping unchanged pages inserts zero rows. `competitor_changes` in `backend/routers/insights.py` infers "sessions" from `scraped_at` timestamps within a 2-hour window — with dedup, the "latest session" can be an old one and the diff compares stale data against itself.

**Do:**
1. Add a `scrape_sessions` table to the schema:
   ```sql
   create table if not exists scrape_sessions (
     id          uuid primary key default gen_random_uuid(),
     source_id   uuid not null references sources(id) on delete cascade,
     started_at  timestamptz not null default now(),
     finished_at timestamptz,
     pages       integer default 0,
     new_chunks  integer default 0,
     errors      integer default 0
   );
   ```
2. Add `session_id uuid references scrape_sessions(id)` and `last_seen_at timestamptz` columns to `scraped_content`.
3. In `scrape_source` / `_scrape_feed`: create a session row at start, pass `session_id` down to `_store_content_chunks`, update session totals at the end.
4. In `_store_content_chunks`: when a chunk's hash already exists, UPDATE that row's `last_seen_at` and `session_id` instead of skipping silently. New chunks get inserted with the session id.
5. Rewrite `competitor_changes` to compare the two most recent `scrape_sessions` per source: recent chunks = rows with the latest `session_id`, old chunks = rows with the previous `session_id`. Remove the 2-hour-window heuristic.

**Verify:** Scrape a source twice with no content change → two session rows exist, the diff endpoint reports "no changes" (not a comparison of stale data). Change something → diff detects it.

---

## Task 3 — Scope content dedup per source (HIGH)

**Problem:** `scraped_content.content_hash` has a global unique constraint, so identical text on two different sources (or two pages) is attributed only to whichever was scraped first.

**Do:**
1. Change the constraint to `unique (source_id, content_hash)` in the schema (drop the old constraint in the migration snippet).
2. Update the existence check in `_store_content_chunks` to filter by both `source_id` and `content_hash`.

**Verify:** Two sources containing identical boilerplate text each get their own chunk rows.

---

## Task 4 — Add API authentication (CRITICAL)

**Problem:** All endpoints are unauthenticated. Anyone can trigger scrapes, burn OpenAI credits via `/api/insights/chat`, and delete sources.

**Do:**
1. Add `api_auth_key: str` to `Settings` in `backend/config.py`, and `API_AUTH_KEY=` to `.env.example`.
2. Add a FastAPI dependency (e.g. `backend/auth.py`) that checks an `X-API-Key` header against the setting; return 401 on mismatch or absence.
3. Apply it to all three routers via `dependencies=[Depends(require_api_key)]` in `backend/main.py`. Leave `/` (frontend) and `/docs` accessible.
4. Frontend: in the single fetch helper (~line 744 of `frontend/index.html`), send the header. Simplest approach: prompt once for the key, keep it in a JS variable in memory, include it on every request.

**Verify:** Requests without the header get 401; with the correct key, everything works.

---

## Task 5 — Fix SSRF in add-url endpoint (CRITICAL)

**Problem:** `POST /api/sources/{id}/add-url` in `backend/routers/sources.py` fetches any caller-supplied URL, including internal network addresses (cloud metadata endpoints, internal services).

**Do:**
1. Add a validator (e.g. in `backend/services/scraper.py`): scheme must be http/https; resolve the hostname and reject if any resolved IP is private, loopback, link-local, or reserved (use `ipaddress` module against all `socket.getaddrinfo` results).
2. Apply it in `add_url_to_source` before fetching, and also at the start of `scrape_source` (source URLs are user input too). Return 400 with a clear message.

**Verify:** `http://169.254.169.254/`, `http://localhost:8000/`, and `file://` URLs are rejected; normal public URLs still work.

---

## Task 6 — Fix feed detection swallowing sitemaps (HIGH)

**Problem:** `_is_feed_url` in `backend/services/scraper.py` treats any `.xml` path as an RSS/Atom feed, so adding `https://site.com/sitemap.xml` as a source routes to the feed parser and errors.

**Do:**
1. In `_is_feed_url`, return False if `sitemap` appears in the path.
2. Better: after fetching, sniff the XML root element — `<rss>`/`<feed>` → feed parser; `<urlset>`/`<sitemapindex>` → treat as sitemap seed list for the crawler.

**Verify:** A sitemap.xml URL added as a source crawls its pages; a real RSS feed still parses as a feed.

---

## Task 7 — Expire stale content on rescrape (HIGH)

**Problem:** Old chunks accumulate forever; removed pages remain in RAG context, so `/api/insights/chat` can answer from outdated pricing/messaging.

**Do (builds on Task 2):**
1. After a successful full-crawl session, delete `scraped_content` rows for that source whose `last_seen_at` predates the session start (i.e. pages that no longer exist or content that changed). Only do this when the session completed without a high error rate (e.g. errors < 20% of pages), to avoid wiping data on a partially failed crawl.
2. Alternatively/additionally: in `match_content` usage (`get_relevant_context` in `backend/services/llm.py`), prefer recent chunks — add an optional recency filter parameter.

**Verify:** Scrape, remove a page's content from the source site (or simulate), rescrape → the old chunk is gone and chat no longer cites it.

---

## Task 8 — Align similarity thresholds (HIGH)

**Problem:** `match_content` in SQL defaults to 0.4 but `get_relevant_context` in `backend/services/llm.py` passes 0.1 — weak matches dilute RAG answers.

**Do:**
1. Add `match_threshold: float = 0.35` to `Settings` in `backend/config.py` (env-overridable) and use it in `get_relevant_context`.
2. Keep the SQL default in sync.

**Verify:** Chat responses cite only meaningfully relevant sources; a nonsense query returns "no relevant information" rather than random chunks.

---

## Task 9 — Prevent concurrent scrapes of the same source (HIGH)

**Problem:** The hourly scheduler (`backend/scheduler.py`) and manual triggers (`backend/routers/scraper.py`) can scrape the same source simultaneously — duplicate work, confusing status.

**Do:**
1. At the start of `scrape_source` and `_scrape_feed`, check `scrape_status.get_status(source_id)`; if state is `running`, log and return `{"skipped": true, "reason": "already running"}`.
2. In `run_scrape` router endpoint, return 409 with a friendly message if already running.
3. Ensure status is always set to `completed`/`error` in a `finally` block so a crashed scrape doesn't lock the source forever. Add a staleness escape hatch: if a `running` status is older than e.g. 30 minutes, allow a new scrape.

**Verify:** Triggering two scrapes back-to-back → second is rejected/skipped; after an exception mid-scrape, the source can be scraped again.

---

## Task 10 — Batch chunk existence checks (MEDIUM)

**Problem:** `_store_content_chunks` does one SELECT per chunk (N+1).

**Do:**
1. Compute all chunk hashes for the page first, fetch existing hashes in one query (`.in_("content_hash", hashes)` filtered by `source_id`), then insert only the missing ones. Batch-insert new records in one call where possible.
2. Keep per-chunk embedding calls (they must be individual), but consider `asyncio.gather` with a small semaphore (e.g. 5 concurrent) for speed.

**Verify:** Scraping a 20-chunk page performs 1 existence query instead of 20; stored data identical to before.

---

## Task 11 — Compute source stats in SQL (MEDIUM)

**Problem:** `list_sources` in `backend/routers/sources.py` paginates the entire `scraped_content` table in Python to count chunks and unique URLs per source.

**Do:**
1. Add a SQL function to the schema:
   ```sql
   create or replace function source_stats()
   returns table (source_id uuid, pages bigint, chunks bigint)
   language sql stable as $$
     select source_id, count(distinct url), count(*)
     from scraped_content group by source_id;
   $$;
   ```
2. Call it via `db.rpc("source_stats")` in `list_sources`; remove the pagination loop.

**Verify:** Endpoint returns identical numbers, in one DB round-trip.

---

## Task 12 — Respect robots.txt when crawling (MEDIUM)

**Problem:** robots.txt is read for sitemap hints only; Disallow rules are ignored during the BFS crawl.

**Do:**
1. In `scrape_source`, fetch and parse robots.txt once per crawl using `urllib.robotparser.RobotFileParser` (feed it the fetched text via `parse()` — don't let it fetch synchronously itself).
2. Skip queued URLs that are disallowed for our User-Agent (also honour `*`). Log skipped counts in the summary.

**Verify:** A site disallowing `/private/` never has those paths crawled; sites without robots.txt crawl normally.

---

## Task 13 — Reuse a shared httpx client (MEDIUM)

**Problem:** `fetch_page` and `_fetch_text` create a new `AsyncClient` per request — wasteful connection setup.

**Do:**
1. Create a module-level client in `backend/services/scraper.py` (lazy-init, with `_HEADERS`, `follow_redirects=True`).
2. Close it in the FastAPI lifespan shutdown (`backend/main.py`).

**Verify:** All scraping still works; no "client closed" errors on shutdown.

---

## Task 14 — Harden the scheduler loop (MEDIUM)

**Problem:** In `backend/scheduler.py`, an unhandled exception in `_loop` kills the background task silently; also the inner loop reuses the `result` variable name confusingly.

**Do:**
1. Wrap the `_check_and_scrape()` call in `_loop` with try/except (log and continue), so one bad cycle never kills the loop. Let `asyncio.CancelledError` propagate.
2. Rename the inner `result` (scrape outcome) to `outcome` for clarity.
3. In `stop_scheduler`, await the cancelled task where feasible for clean shutdown.

**Verify:** Force an exception inside `_check_and_scrape` → the loop logs it and runs again next hour.

---

## Task 15 — Return 404 from delete_source for unknown IDs (LOW)

**Problem:** `DELETE /api/sources/{id}` returns 204 even when nothing was deleted.

**Do:** Check `result.data` after the delete; raise `HTTPException(404, "Source not found.")` if empty.

**Verify:** Deleting a random UUID → 404; deleting a real source → 204.

---

## Task 16 — Add tests for pure functions (LOW)

**Problem:** Zero tests.

**Do:**
1. Add `pytest` + `pytest-asyncio` to a new `requirements-dev.txt`.
2. Create `tests/` with unit tests for: `chunk_text` (short text, exact boundary, overlap correctness), `extract_links` (same-domain filter, fragment stripping, skip extensions, mailto/js links), `extract_content` (title fallback chain, tag stripping), `_parse_sitemap_xml` (urlset, sitemapindex, malformed XML), `_parse_feed_xml` (RSS 2.0, Atom, content:encoded preference, malformed XML), `_is_feed_url` (including the sitemap case from Task 6), and the SSRF validator from Task 5.
3. No network or DB in unit tests — use inline fixture strings.

**Verify:** `pytest` passes; aim for the parsers and validators fully covered.

---

## Task 17 — Split frontend JS into modules (LOW)

**Problem:** `frontend/index.html` is 2,241 lines with all JS inline.

**Do:**
1. Extract JS into `frontend/js/` modules (e.g. `api.js` for the fetch helper, one file per tab/feature) using native ES modules (`<script type="module">`). Extract CSS to `frontend/css/styles.css`.
2. Update `backend/main.py` to serve the frontend directory statically (`StaticFiles`) so the extra files resolve.
3. No behaviour changes — pure refactor.

**Verify:** Every tab and action works identically; no console errors.

---

## Suggested commit sequence

Tasks 1–3 belong together (one schema migration). Then 4–5 (security), 6–9 (correctness), 10–14 (perf/robustness), 15–17 (hygiene). Run the app and click through the UI after each group.
