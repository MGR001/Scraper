-- ============================================================
-- 006_page_summaries.sql
-- Idempotent – run AFTER 001-005.
-- Adds a stored, per-page summary layer between the scraper and
-- everything that consumes content (frameworks, chat, digest).
-- ============================================================

-- ── 1. page_summaries table ─────────────────────────────────
create table if not exists page_summaries (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid not null references workspaces(id) on delete cascade,
  source_id     uuid not null references sources(id)    on delete cascade,
  url           text not null,
  title         text,
  page_type     text not null default 'other'
                  check (page_type in
                    ('home','pricing','product','solutions','customers',
                     'blog','news','about','careers','legal','other')),
  summary       text not null,
  embedding     vector(1536),
  session_id    uuid references scrape_sessions(id),
  content_hash  text,              -- hash of the combined page content the summary was built from
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (source_id, url)
);

create index if not exists idx_page_summaries_workspace on page_summaries(workspace_id);
create index if not exists idx_page_summaries_source    on page_summaries(source_id);
create index if not exists idx_page_summaries_type      on page_summaries(source_id, page_type);
-- vector index for semantic search over summaries
create index if not exists idx_page_summaries_embedding
  on page_summaries using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- ── 2. RLS ───────────────────────────────────────────────────
alter table page_summaries enable row level security;

drop policy if exists "page_summaries_workspace" on page_summaries;

create policy "page_summaries_workspace" on page_summaries
  for all
  using     (is_workspace_member(workspace_id))
  with check (is_workspace_member(workspace_id));

-- ── 3. match_page_summaries RPC ─────────────────────────────
-- Mirrors match_content (see migrations/002_workspace_columns.sql) with an
-- added optional p_source_id filter for scoping to a single company.
create or replace function match_page_summaries(
  query_embedding  vector(1536),
  match_threshold  float   default 0.35,
  match_count      int     default 8,
  p_workspace_id   uuid    default null,   -- null = legacy call (service key only)
  p_source_id      uuid    default null    -- null = no source filter
)
returns table (
  id          uuid,
  source_id   uuid,
  url         text,
  title       text,
  page_type   text,
  summary     text,
  updated_at  timestamptz,
  similarity  float
)
language sql stable as $$
  select
    ps.id,
    ps.source_id,
    ps.url,
    ps.title,
    ps.page_type,
    ps.summary,
    ps.updated_at,
    1 - (ps.embedding <=> query_embedding) as similarity
  from page_summaries ps
  where ps.embedding is not null
    and (p_workspace_id is null or ps.workspace_id = p_workspace_id)
    and (p_source_id is null or ps.source_id = p_source_id)
    and 1 - (ps.embedding <=> query_embedding) > match_threshold
  order by ps.embedding <=> query_embedding
  limit match_count;
$$;

-- ── 4. Cost visibility: summaries generated per sweep (Task 7) ─
alter table scrape_sessions
  add column if not exists summaries_generated integer default 0;
