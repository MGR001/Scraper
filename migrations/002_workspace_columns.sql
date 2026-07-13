-- ============================================================
-- 002_workspace_columns.sql
-- Idempotent – run AFTER 001_multitenancy.sql.
-- Adds workspace_id to all data tables, backfills existing rows,
-- makes it NOT NULL, and scopes uniqueness constraints per tenant.
-- ============================================================

-- ── 1. Add workspace_id columns (nullable first) ──────────
alter table sources
  add column if not exists workspace_id uuid references workspaces(id) on delete cascade;

alter table scraped_content
  add column if not exists workspace_id uuid references workspaces(id) on delete cascade;

alter table scrape_sessions
  add column if not exists workspace_id uuid references workspaces(id) on delete cascade;

-- ── 2. Indexes ─────────────────────────────────────────────
create index if not exists idx_sources_workspace  on sources(workspace_id);
create index if not exists idx_content_workspace  on scraped_content(workspace_id);
create index if not exists idx_sessions_workspace on scrape_sessions(workspace_id);

-- ── 3. Workspace settings columns ─────────────────────────
alter table workspaces
  add column if not exists scrape_enabled    boolean  not null default true,
  add column if not exists scrape_frequency  text     not null default 'daily'
    check (scrape_frequency in ('hourly','daily','weekly')),
  add column if not exists scrape_hour       int      not null default 4,
  add column if not exists timezone          text     not null default 'UTC',
  add column if not exists crawl_max_pages   int      not null default 50,
  add column if not exists slack_webhook_url text;

-- ── 4. Per-user preferences ────────────────────────────────
create table if not exists user_preferences (
  user_id            uuid not null references profiles(id)   on delete cascade,
  workspace_id       uuid not null references workspaces(id) on delete cascade,
  email_digest       boolean not null default true,
  digest_frequency   text    not null default 'daily'
    check (digest_frequency in ('daily','weekly','off')),
  notify_pricing     boolean not null default true,
  notify_messaging   boolean not null default true,
  notify_sentiment   boolean not null default true,
  quiet_when_nothing boolean not null default true,
  theme              text    not null default 'system'
    check (theme in ('light','dark','system')),
  primary key (user_id, workspace_id)
);

-- ── 5. Backfill existing rows onto a default workspace ─────
-- Only runs when unassigned rows exist (safe to re-run).
do $$
declare
  v_workspace_id uuid;
  v_user_id      uuid;
begin
  if not exists (select 1 from sources where workspace_id is null) then
    return;  -- nothing to backfill
  end if;

  -- Use the oldest auth.users row as the workspace owner
  select id into v_user_id from auth.users order by created_at limit 1;
  if v_user_id is null then
    raise notice '002: no auth users found – skipping backfill';
    return;
  end if;

  -- Ensure profile exists
  insert into profiles (id, email)
  select id, email from auth.users where id = v_user_id
  on conflict (id) do nothing;

  -- Create (or reuse) the default workspace
  insert into workspaces (name, slug, created_by)
  values ('My Workspace', 'my-workspace', v_user_id)
  on conflict (slug) do nothing;

  select id into v_workspace_id from workspaces where slug = 'my-workspace' limit 1;

  -- Ensure owner membership
  insert into workspace_members (workspace_id, user_id, role)
  values (v_workspace_id, v_user_id, 'owner')
  on conflict do nothing;

  -- Stamp all unassigned rows
  update sources          set workspace_id = v_workspace_id where workspace_id is null;
  update scraped_content  set workspace_id = v_workspace_id where workspace_id is null;
  update scrape_sessions  set workspace_id = v_workspace_id where workspace_id is null;

  raise notice '002: backfilled existing rows to workspace %', v_workspace_id;
end;
$$;

-- ── 6. Make workspace_id NOT NULL ──────────────────────────
alter table sources         alter column workspace_id set not null;
alter table scraped_content alter column workspace_id set not null;
alter table scrape_sessions alter column workspace_id set not null;

-- ── 7. Scope uniqueness constraints per tenant ─────────────

-- sources.url: was globally unique, now unique per workspace
alter table sources drop constraint if exists sources_url_unique;
alter table sources drop constraint if exists sources_workspace_url_unique;
alter table sources add constraint sources_workspace_url_unique
  unique (workspace_id, url);

-- scraped_content: already per (source_id, content_hash), add workspace
-- The old uniqueness may exist either as a table constraint (whose backing
-- index can't be dropped directly) or as a bare index, depending on which
-- path supabase_setup.sql took — handle both without erroring.
do $$
begin
  if exists (
    select 1 from pg_constraint
    where conname = 'scraped_content_source_hash_unique'
      and conrelid = 'scraped_content'::regclass
  ) then
    alter table scraped_content drop constraint scraped_content_source_hash_unique;
  elsif exists (
    select 1 from pg_indexes
    where schemaname = 'public'
      and tablename = 'scraped_content'
      and indexname = 'scraped_content_source_hash_unique'
  ) then
    drop index scraped_content_source_hash_unique;
  end if;
end $$;

create unique index if not exists scraped_content_ws_src_hash_unique
  on scraped_content (workspace_id, source_id, content_hash);

-- ── 8. Update match_content to be workspace-scoped ─────────
-- The original function crosses all tenants – this is the key security fix.
create or replace function match_content(
  query_embedding  vector(1536),
  match_threshold  float   default 0.35,
  match_count      int     default 8,
  p_workspace_id   uuid    default null   -- null = legacy call (service key only)
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
language sql stable as $$
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
    and (p_workspace_id is null or sc.workspace_id = p_workspace_id)
    and 1 - (sc.embedding <=> query_embedding) > match_threshold
  order by sc.embedding <=> query_embedding
  limit match_count;
$$;
