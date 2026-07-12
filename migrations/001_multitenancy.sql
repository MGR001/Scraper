-- ============================================================
-- 001_multitenancy.sql
-- Idempotent – safe to run on any existing RIvals database.
-- Run in Supabase SQL Editor BEFORE migrations 002 and 003.
-- ============================================================

-- ── 1. Profiles (one row per auth.users row) ──────────────
create table if not exists profiles (
  id           uuid primary key references auth.users(id) on delete cascade,
  email        text not null,
  full_name    text,
  avatar_url   text,
  created_at   timestamptz not null default now()
);

-- ── 2. Workspaces (the tenant unit) ───────────────────────
create table if not exists workspaces (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,
  slug          text unique not null,
  company_name  text,
  company_url   text,
  created_by    uuid not null references profiles(id),
  created_at    timestamptz not null default now(),
  onboarded_at  timestamptz          -- null = onboarding not yet finished
);

-- ── 3. Workspace membership ────────────────────────────────
create table if not exists workspace_members (
  workspace_id  uuid not null references workspaces(id) on delete cascade,
  user_id       uuid not null references profiles(id)   on delete cascade,
  role          text not null default 'member'
                  check (role in ('owner','admin','member','viewer')),
  joined_at     timestamptz not null default now(),
  primary key (workspace_id, user_id)
);
create index if not exists idx_members_user on workspace_members(user_id);

-- ── 4. Workspace invites ───────────────────────────────────
create table if not exists workspace_invites (
  id            uuid primary key default gen_random_uuid(),
  workspace_id  uuid not null references workspaces(id) on delete cascade,
  email         text not null,
  role          text not null default 'member'
                  check (role in ('admin','member','viewer')),
  token         text unique not null default encode(gen_random_bytes(24), 'hex'),
  invited_by    uuid not null references profiles(id),
  expires_at    timestamptz not null default (now() + interval '7 days'),
  accepted_at   timestamptz,
  created_at    timestamptz not null default now()
);
create index if not exists idx_invites_workspace on workspace_invites(workspace_id);
create index if not exists idx_invites_token     on workspace_invites(token);

-- ── 5. Auto-create profile row when a user signs up ───────
create or replace function handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, email, full_name)
  values (
    new.id,
    new.email,
    new.raw_user_meta_data->>'full_name'
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();

-- ── 6. Helper used by RLS policies ────────────────────────
create or replace function is_workspace_member(ws uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from workspace_members
    where workspace_id = ws and user_id = auth.uid()
  );
$$;

create or replace function workspace_role(ws uuid)
returns text language sql stable security definer set search_path = public as $$
  select role from workspace_members
  where workspace_id = ws and user_id = auth.uid()
  limit 1;
$$;
