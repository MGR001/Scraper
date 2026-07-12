-- StrategyHub Database Setup
-- Run the entire contents of this file in your Supabase SQL Editor
-- (https://supabase.com/dashboard → SQL Editor → New query)

-- 1. Enable the pgvector extension
create extension if not exists vector;

-- 2. Sources: websites to monitor
create table if not exists sources (
  id                   uuid primary key default gen_random_uuid(),
  name                 text not null,
  url                  text not null,
  category             text not null default 'general',  -- competitor | news | market | general
  scrape_interval      integer not null default 24,       -- hours between scrapes
  last_scraped_at      timestamptz,
  is_active            boolean not null default true,
  created_at           timestamptz default now(),
  sitemap_url          text,
  summary              text,
  summary_generated_at timestamptz,
  constraint sources_url_unique unique (url)
);

-- 3. Scrape sessions: one row per crawl/feed-fetch run
create table if not exists scrape_sessions (
  id          uuid primary key default gen_random_uuid(),
  source_id   uuid not null references sources(id) on delete cascade,
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  pages       integer default 0,
  new_chunks  integer default 0,
  errors      integer default 0
);

-- 4. Scraped content (chunked for RAG)
create table if not exists scraped_content (
  id           uuid primary key default gen_random_uuid(),
  source_id    uuid not null references sources(id) on delete cascade,
  session_id   uuid references scrape_sessions(id),
  url          text not null,
  title        text,
  content      text not null,
  content_hash text not null,
  embedding    vector(1536),             -- text-embedding-3-small dimensions
  scraped_at   timestamptz default now(),
  last_seen_at timestamptz default now(),
  metadata     jsonb not null default '{}'::jsonb,
  constraint scraped_content_source_hash_unique unique (source_id, content_hash)
);

-- 5. IVFFlat index for fast approximate vector search
create index if not exists scraped_content_embedding_idx
  on scraped_content using ivfflat (embedding vector_cosine_ops)
  with (lists = 100);

-- 6. Helper function: find the most relevant content chunks for a query embedding
create or replace function match_content(
  query_embedding vector(1536),
  match_threshold float default 0.4,
  match_count     int   default 8
)
returns table (
  id          uuid,
  source_id   uuid,
  url         text,
  title       text,
  content     text,
  scraped_at  timestamptz,
  similarity  float
)
language sql stable
as $$
  select
    sc.id,
    sc.source_id,
    sc.url,
    sc.title,
    sc.content,
    sc.scraped_at,
    1 - (sc.embedding <=> query_embedding) as similarity
  from scraped_content sc
  where sc.embedding is not null
    and 1 - (sc.embedding <=> query_embedding) > match_threshold
  order by sc.embedding <=> query_embedding
  limit match_count;
$$;

-- ============================================================
-- Migration: apply to existing databases (idempotent)
-- ============================================================
alter table sources
  add column if not exists sitemap_url          text,
  add column if not exists summary              text,
  add column if not exists summary_generated_at timestamptz;

create table if not exists scrape_sessions (
  id          uuid primary key default gen_random_uuid(),
  source_id   uuid not null references sources(id) on delete cascade,
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  pages       integer default 0,
  new_chunks  integer default 0,
  errors      integer default 0
);

alter table scraped_content
  add column if not exists session_id   uuid references scrape_sessions(id),
  add column if not exists last_seen_at timestamptz default now();

-- Drop the old global unique constraint and replace with per-source dedup
alter table scraped_content
  drop constraint if exists scraped_content_hash_unique;
create unique index if not exists scraped_content_source_hash_unique
  on scraped_content (source_id, content_hash);
