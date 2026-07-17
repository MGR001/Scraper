# Rivalry — Reddit Sentiment Ingestion (tasks-reddit.md)

Build the mentions pipeline: poll Reddit for posts and comments that mention watched competitors, classify each mention (relevance, sentiment, aspect, signal type), and store them as structured rows in a new `mentions` table. This is the customer-voice layer — it feeds the sentiment heatmap, the digest, and (later) Kano/battlecard evidence.

**Hard rules for this whole file:**
- Use Reddit's **JSON endpoints** (`*.json`), never scrape the HTML UI.
- **Never** route Reddit content through the scraper→chunks→embeddings pipeline. Mentions are structured data with their own table and their own classifier.
- Every Reddit request sends a descriptive User-Agent: `Rivalry/1.0 (competitive intelligence; <contact email from env>)`. Reddit blocks default agents.
- Max ~1 request/second, unauthenticated. Build the client so OAuth can be added later without touching callers.

Work through tasks in order; complete, verify, commit each.

---

## Task 1 — Schema: `mentions` + monitoring config

Create `migrations/00X_mentions.sql` (next free number, idempotent):

```sql
create table if not exists mentions (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid not null references workspaces(id) on delete cascade,
  source_id     uuid not null references sources(id)    on delete cascade,  -- the competitor mentioned
  platform      text not null default 'reddit' check (platform in ('reddit','hackernews')),
  external_id   text not null,          -- reddit fullname: t3_xxx (post) / t1_xxx (comment)
  parent_id     text,                   -- t3_xxx of the thread a comment belongs to
  kind          text not null check (kind in ('post','comment')),
  url           text not null,          -- permalink
  subreddit     text,
  author        text,
  title         text,                   -- thread title (posts and comments both carry it, for context)
  body          text,
  score         integer default 0,      -- upvotes at fetch time
  published_at  timestamptz,
  fetched_at    timestamptz not null default now(),

  -- classifier outputs (null until classified)
  relevant      boolean,
  confidence    float,
  sentiment     float,                  -- -1 .. 1
  aspect        text check (aspect in
                  ('pricing','support','product','onboarding','reliability','docs','other')),
  signal_type   text check (signal_type in
                  ('complaint','praise','question','comparison','switching_intent','other')),
  is_firsthand  boolean,
  summary       text,

  unique (platform, external_id, source_id)   -- one thread can mention two rivals → one row per rival
);

create index if not exists idx_mentions_workspace on mentions(workspace_id);
create index if not exists idx_mentions_source_time on mentions(source_id, published_at desc);
create index if not exists idx_mentions_signal on mentions(workspace_id, signal_type)
  where relevant = true;

alter table mentions enable row level security;
drop policy if exists "mentions_workspace" on mentions;
create policy "mentions_workspace" on mentions
  for all using (is_workspace_member(workspace_id))
  with check (is_workspace_member(workspace_id));
```

Also add monitoring config to `sources` (competitor sources only, used by the poller):

```sql
alter table sources
  add column if not exists mention_terms       text[],   -- e.g. {"legora","leya"} — defaults to name
  add column if not exists mention_subreddits  text[],   -- e.g. {"legaltech","LawFirm"}
  add column if not exists mentions_enabled    boolean not null default false,
  add column if not exists mentions_checked_at timestamptz;
```

And per-subreddit stream state (so "poll r/legaltech/new" knows its high-water mark), workspace-scoped:

```sql
create table if not exists mention_streams (
  workspace_id  uuid not null references workspaces(id) on delete cascade,
  platform      text not null default 'reddit',
  stream_key    text not null,          -- e.g. 'r/legaltech/new'
  last_seen_utc bigint default 0,       -- created_utc of newest processed post
  primary key (workspace_id, platform, stream_key)
);
alter table mention_streams enable row level security;
drop policy if exists "mention_streams_workspace" on mention_streams;
create policy "mention_streams_workspace" on mention_streams
  for all using (is_workspace_member(workspace_id))
  with check (is_workspace_member(workspace_id));
```

**Verify:** migration idempotent; RLS blocks cross-workspace reads.

---

## Task 2 — Reddit client

Create `backend/services/reddit.py`. Pure fetch layer — no classification, no DB.

1. Module-level shared `httpx.AsyncClient` with the User-Agent header (contact email from a new `CONTACT_EMAIL` setting in config + `.env.example`). Simple politeness throttle: an `asyncio.Lock` + timestamp ensuring ≥1.0s between requests.

2. Functions (all return parsed dicts, all raise a single `RedditError` on failure):

   ```python
   async def fetch_subreddit_new(subreddit: str, limit: int = 100) -> list[dict]
       # GET https://www.reddit.com/r/{subreddit}/new.json?limit={limit}
       # returns list of post dicts: id, name, title, selftext, author, score,
       # num_comments, created_utc, permalink, subreddit

   async def search_mentions(term: str, subreddit: str | None = None, limit: int = 50) -> list[dict]
       # subreddit given:  /r/{sub}/search.json?q="{term}"&restrict_sr=1&sort=new&limit=...
       # no subreddit:     /search.json?q="{term}"&sort=new&limit=...
       # quote multi-word terms

   async def fetch_comments(post_id: str, max_comments: int = 60) -> dict
       # GET https://www.reddit.com/comments/{post_id}.json
       # returns {"post": {...}, "comments": [flat list]}
   ```

3. **Comment-tree walker** inside `fetch_comments`: the second listing element contains nested comments; recurse through `data.children`, collecting each comment's `id, name, author, body, score, created_utc, permalink`. Skip `kind == "more"` stubs (do NOT fetch them in v1). Skip bodies that are `[deleted]` or `[removed]`. Stop at `max_comments`. Attach the thread title to every comment dict.

4. Handle 429 (back off 30s, retry once) and 403/blocked (raise with clear message).

**Verify:** unit tests with recorded/fixture JSON for the tree walker (nested replies, `more` stubs, deleted comments). Live smoke test against r/legaltech works with the proper User-Agent.

---

## Task 3 — Mention classifier

Create `backend/services/mention_classifier.py`.

1. `async def classify_mention(competitor_name: str, terms: list[str], thread_title: str, body: str, is_comment: bool) -> dict`

2. Use the **cheap model** (reuse `summary_model` from Settings or add `classifier_model`; never the main chat model — this is high-volume work).

3. System prompt (base — keep it strict JSON):

   ```
   You classify a Reddit post or comment for competitive intelligence about
   the company "{competitor_name}" (also known as: {terms}).

   Return ONLY valid JSON:
   {"relevant": bool, "confidence": 0..1, "sentiment": -1..1,
    "aspect": "pricing|support|product|onboarding|reliability|docs|other",
    "signal_type": "complaint|praise|question|comparison|switching_intent|other",
    "is_firsthand": bool, "summary": "<max 25 words>"}

   relevant=false if: the text is not about this company as a product/service,
   is the company's own marketing or an employee, or the name match is
   coincidental. When relevant=false, other fields may be null.
   is_firsthand=true only if the author describes their own direct experience.
   signal_type=switching_intent when the author states they are leaving,
   have left, or are actively evaluating alternatives to this company.
   Reddit sarcasm is common — judge tone, not surface words.
   Use the thread title for context; a short comment like "same here"
   inherits the meaning of the thread.
   ```

4. Parse defensively (strip fences, validate enums, clamp numeric ranges; on parse failure return `relevant=None` and log — never raise).

5. `async def classify_and_store(db, workspace_id, source, item: dict, kind: str) -> bool` — dedupe first (`platform, external_id, source_id` exists → skip, zero LLM cost), classify, insert. Returns whether an LLM call was made.

**Verify:** unit tests for parsing + enum validation. Then the **validation pass** (do this personally, not the agent): run the classifier over ~100 real mentions, export to CSV, hand-check `relevant` precision and gross sentiment direction. If relevance precision < ~85%, tighten the prompt before proceeding to Task 5.

---

## Task 4 — Ingestion service

Create `backend/services/mention_monitor.py` with one entry point:

```python
async def check_mentions_for_workspace(db, workspace_id) -> dict   # returns counts
```

Per competitor source with `mentions_enabled`:

1. **Targeted search:** for each term × each configured subreddit (and one global search per term), call `search_mentions`, process posts newer than `mentions_checked_at`.
2. **Stream watch:** for each distinct subreddit across the workspace's sources, poll `fetch_subreddit_new` once (not per source), using `mention_streams.last_seen_utc` as the high-water mark; keep posts whose title/selftext contains any watched term (case-insensitive) OR with `num_comments > 3` and a term appearing in the thread later.
3. **Comment expansion:** for each kept post, call `fetch_comments` and classify post + each comment against every competitor whose terms appear in that item's text (an item mentioning two rivals produces two rows — the unique constraint handles it).
4. Update `mentions_checked_at` per source and `last_seen_utc` per stream **only after** successful processing.
5. **Budget guard:** `max_mention_classifications_per_sweep` setting (default 200). Beyond it, stop and log — same pattern as the summary cap.
6. Wrap everything per-source in try/except; one competitor's failure must not block the rest. Log one line per run: `mentions: X fetched, Y classified, Z relevant, W skipped(dedupe)`.

**Scheduler hook:** in `backend/scheduler.py`, after each workspace's scrape sweep, call `check_mentions_for_workspace` (service-role client, same isolation as the scraper). Respect `scrape_enabled`.

**Verify:** two sweeps back-to-back → second one classifies ~zero (dedupe + high-water marks working). A post mentioning two watched rivals produces one row per rival.

---

## Task 5 — API + minimal UI

1. `backend/routers/mentions.py`:
   - `GET /api/mentions` — feed; filters: `source_id`, `signal_type`, `aspect`, `min_sentiment`/`max_sentiment`, `since`; `relevant=true` only by default; ordered `published_at desc`; paginated.
   - `GET /api/mentions/summary` — per source: mention count, weighted sentiment, count of `switching_intent`, top negative aspect. Weight sentiment by `ln(1 + greatest(score,0))`; **suppress** any aggregate built on fewer than 5 relevant mentions (return `n` and `insufficient: true` instead of a number).
   - `PATCH /api/sources/{id}/mentions-config` — set `mention_terms`, `mention_subreddits`, `mentions_enabled`.
   - Auth + workspace scoping identical to other routers.

2. Frontend, minimal v1:
   - On the source edit form: mentions enable toggle, terms input (chips), subreddits input.
   - A **Mentions** view: the feed with sentiment/signal chips (reuse existing chip styles), each row linking to the Reddit permalink, filter by competitor and signal type. A "Switching intent" quick-filter button.
   - Show "Not enough signal yet" wherever `insufficient: true` — never render a number built on <5 mentions.

**Verify:** enable mentions for one competitor with terms + r/legaltech; run a sweep; the feed shows classified mentions with working permalinks; summary suppresses low-n aggregates.

---

## Task 6 — Guard rails & honesty

1. **No fake neutrality:** UIs must distinguish "no data" from "sentiment 0.0" everywhere.
2. **Spike flag (cheap version):** in `/api/mentions/summary`, include `spike: true` when the last 24h relevant-mention count exceeds 5× the trailing 7-day daily average (min 5 mentions). Display as a badge, not an alert system — full anomaly detection is out of scope.
3. **Tests:** `tests/test_mentions.py` — dedupe on unique constraint, weighted-sentiment math, low-n suppression, tenancy isolation (workspace A cannot read B's mentions).
4. **Do not** add Slack/email delivery of mentions in this file — that belongs to the digest work.
5. Homepage: do NOT re-add sentiment claims yet. Only after this ships and the validation pass in Task 3 is done.

---

## Commit sequence

| Commit | Tasks |
|---|---|
| 1 | 1 (schema) |
| 2 | 2 (reddit client) |
| 3 | 3 (classifier) — then STOP for the manual 100-mention validation pass |
| 4 | 4 (ingestion + scheduler) |
| 5 | 5–6 (API, UI, guard rails) |

The stop after commit 3 is deliberate: do not wire the classifier into automated sweeps until a human has checked ~100 classifications. A wrong sentiment number shown confidently is worse than no sentiment feature.