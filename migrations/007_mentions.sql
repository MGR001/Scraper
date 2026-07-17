-- ============================================================
-- 007_mentions.sql
-- Idempotent – run AFTER 001-006.
-- Reddit sentiment ingestion: structured mentions table, per-source
-- monitoring config, and per-subreddit stream high-water marks.
-- Reddit content never enters the scraper→chunks→embeddings pipeline;
-- this is its own table with its own classifier.
-- ============================================================

-- ── 1. mentions table ───────────────────────────────────────
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

-- ── 2. Monitoring config on sources (competitor sources only) ─
alter table sources
  add column if not exists mention_terms       text[],   -- e.g. {"legora","leya"} — defaults to name
  add column if not exists mention_subreddits  text[],   -- e.g. {"legaltech","LawFirm"}
  add column if not exists mentions_enabled    boolean not null default false,
  add column if not exists mentions_checked_at timestamptz;

-- ── 3. Per-subreddit stream state (high-water mark), workspace-scoped ─
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
