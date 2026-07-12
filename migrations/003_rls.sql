-- ============================================================
-- 003_rls.sql
-- Idempotent – run AFTER 001 and 002.
-- Enables RLS and adds access policies for all tables.
-- ============================================================

-- ── Enable RLS ─────────────────────────────────────────────
alter table profiles          enable row level security;
alter table workspaces        enable row level security;
alter table workspace_members enable row level security;
alter table workspace_invites enable row level security;
alter table user_preferences  enable row level security;
alter table sources           enable row level security;
alter table scraped_content   enable row level security;
alter table scrape_sessions   enable row level security;

-- ══════════════════════════════════════════════════════════
-- profiles
-- ══════════════════════════════════════════════════════════
drop policy if exists "profiles_select_own"  on profiles;
drop policy if exists "profiles_update_own"  on profiles;

create policy "profiles_select_own" on profiles
  for select using (id = auth.uid());

create policy "profiles_update_own" on profiles
  for update using (id = auth.uid());

-- ══════════════════════════════════════════════════════════
-- workspaces
-- ══════════════════════════════════════════════════════════
drop policy if exists "workspaces_select_member"     on workspaces;
drop policy if exists "workspaces_insert_any_authed" on workspaces;
drop policy if exists "workspaces_update_admin"      on workspaces;
drop policy if exists "workspaces_delete_owner"      on workspaces;

create policy "workspaces_select_member" on workspaces
  for select using (is_workspace_member(id));

-- Any authenticated user can create a workspace (they become owner in the same txn)
create policy "workspaces_insert_any_authed" on workspaces
  for insert with check (auth.uid() is not null);

create policy "workspaces_update_admin" on workspaces
  for update using (workspace_role(id) in ('owner','admin'));

create policy "workspaces_delete_owner" on workspaces
  for delete using (workspace_role(id) = 'owner');

-- ══════════════════════════════════════════════════════════
-- workspace_members
-- ══════════════════════════════════════════════════════════
drop policy if exists "members_select"  on workspace_members;
drop policy if exists "members_insert"  on workspace_members;
drop policy if exists "members_delete"  on workspace_members;
drop policy if exists "members_update"  on workspace_members;

create policy "members_select" on workspace_members
  for select using (is_workspace_member(workspace_id));

create policy "members_insert" on workspace_members
  for insert with check (workspace_role(workspace_id) in ('owner','admin'));

create policy "members_update" on workspace_members
  for update using (workspace_role(workspace_id) in ('owner','admin'));

create policy "members_delete" on workspace_members
  for delete using (workspace_role(workspace_id) in ('owner','admin'));

-- ══════════════════════════════════════════════════════════
-- workspace_invites
-- ══════════════════════════════════════════════════════════
drop policy if exists "invites_select"  on workspace_invites;
drop policy if exists "invites_insert"  on workspace_invites;
drop policy if exists "invites_delete"  on workspace_invites;

create policy "invites_select" on workspace_invites
  for select using (is_workspace_member(workspace_id));

create policy "invites_insert" on workspace_invites
  for insert with check (workspace_role(workspace_id) in ('owner','admin'));

create policy "invites_delete" on workspace_invites
  for delete using (workspace_role(workspace_id) in ('owner','admin'));

-- Function to accept an invite without being a member yet (security definer)
create or replace function accept_workspace_invite(p_token text)
returns json language plpgsql security definer set search_path = public as $$
declare
  v_invite workspace_invites%rowtype;
begin
  select * into v_invite
  from workspace_invites
  where token = p_token
    and accepted_at is null
    and expires_at > now();

  if not found then
    raise exception 'Invite not found, already used, or expired.';
  end if;

  insert into workspace_members (workspace_id, user_id, role)
  values (v_invite.workspace_id, auth.uid(), v_invite.role)
  on conflict (workspace_id, user_id) do nothing;

  update workspace_invites set accepted_at = now() where id = v_invite.id;

  return json_build_object('workspace_id', v_invite.workspace_id, 'role', v_invite.role);
end;
$$;

-- ══════════════════════════════════════════════════════════
-- user_preferences
-- ══════════════════════════════════════════════════════════
drop policy if exists "prefs_own" on user_preferences;

create policy "prefs_own" on user_preferences
  for all using (user_id = auth.uid())
  with check (user_id = auth.uid() and is_workspace_member(workspace_id));

-- ══════════════════════════════════════════════════════════
-- sources / scraped_content / scrape_sessions
-- (all data tables: full access to workspace members only)
-- ══════════════════════════════════════════════════════════
drop policy if exists "sources_workspace"   on sources;
drop policy if exists "content_workspace"   on scraped_content;
drop policy if exists "sessions_workspace"  on scrape_sessions;

create policy "sources_workspace" on sources
  for all
  using     (is_workspace_member(workspace_id))
  with check (is_workspace_member(workspace_id));

create policy "content_workspace" on scraped_content
  for all
  using     (is_workspace_member(workspace_id))
  with check (is_workspace_member(workspace_id));

create policy "sessions_workspace" on scrape_sessions
  for all
  using     (is_workspace_member(workspace_id))
  with check (is_workspace_member(workspace_id));
