import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser, WorkspaceContext, get_current_user, get_workspace, require_role
from ..database import get_service_db

router = APIRouter()


def _make_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "workspace"


# ── Create workspace ───────────────────────────────────────
class WorkspaceCreate(BaseModel):
    name: str
    company_name: str = ""
    company_url: str = ""


@router.post("/", status_code=201)
async def create_workspace(
    body: WorkspaceCreate,
    user: CurrentUser = Depends(get_current_user),
):
    db = get_service_db()

    # Ensure profile exists
    await asyncio.to_thread(
        lambda: db.table("profiles")
        .upsert({"id": user.id, "email": user.email}, on_conflict="id")
        .execute()
    )

    slug = _make_slug(body.name)
    # Make slug unique
    base_slug = slug
    for i in range(1, 20):
        exists = await asyncio.to_thread(
            lambda s=slug: db.table("workspaces").select("id").eq("slug", s).execute()
        )
        if not exists.data:
            break
        slug = f"{base_slug}-{i}"

    ws = await asyncio.to_thread(
        lambda: db.table("workspaces").insert({
            "name": body.name,
            "slug": slug,
            "company_name": body.company_name,
            "company_url": body.company_url,
            "created_by": user.id,
        }).execute()
    )
    workspace_id = ws.data[0]["id"]

    await asyncio.to_thread(
        lambda: db.table("workspace_members").insert({
            "workspace_id": workspace_id,
            "user_id": user.id,
            "role": "owner",
        }).execute()
    )
    return ws.data[0]


# ── Get workspace ──────────────────────────────────────────
@router.get("/{workspace_id}")
async def get_workspace_detail(
    workspace_id: str,
    ws: WorkspaceContext = Depends(get_workspace),
):
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.table("workspaces").select("*").eq("id", workspace_id).execute()
    )
    if not result.data:
        raise HTTPException(404, "Workspace not found.")
    members = await asyncio.to_thread(
        lambda: db.table("workspace_members")
        .select("user_id, role, joined_at, profiles(email, full_name)")
        .eq("workspace_id", workspace_id)
        .execute()
    )
    return {**result.data[0], "members": members.data or []}


# ── Update workspace ───────────────────────────────────────
class WorkspaceUpdate(BaseModel):
    name: str | None = None
    company_name: str | None = None
    company_url: str | None = None
    scrape_enabled: bool | None = None
    scrape_frequency: str | None = None
    scrape_hour: int | None = None
    timezone: str | None = None
    crawl_max_pages: int | None = None
    slack_webhook_url: str | None = None
    onboarded_at: str | None = None


@router.patch("/{workspace_id}")
async def update_workspace(
    workspace_id: str,
    body: WorkspaceUpdate,
    ws: WorkspaceContext = Depends(require_role("owner", "admin")),
):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    if not data:
        raise HTTPException(400, "No fields to update.")
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.table("workspaces").update(data).eq("id", workspace_id).execute()
    )
    return result.data[0] if result.data else {}


# ── Invite ─────────────────────────────────────────────────
class InviteCreate(BaseModel):
    email: str
    role: str = "member"


@router.post("/{workspace_id}/invites", status_code=201)
async def create_invite(
    workspace_id: str,
    body: InviteCreate,
    ws: WorkspaceContext = Depends(require_role("owner", "admin")),
    user: CurrentUser = Depends(get_current_user),
):
    if body.role not in ("admin", "member", "viewer"):
        raise HTTPException(400, "Invalid role.")
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.table("workspace_invites").insert({
            "workspace_id": workspace_id,
            "email": body.email,
            "role": body.role,
            "invited_by": user.id,
        }).execute()
    )
    return result.data[0]


# ── Accept invite ──────────────────────────────────────────
@router.post("/invites/{token}/accept")
async def accept_invite(
    token: str,
    user: CurrentUser = Depends(get_current_user),
):
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.rpc("accept_workspace_invite", {"p_token": token}).execute()
    )
    return result.data


# ── Remove member ──────────────────────────────────────────
@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
async def remove_member(
    workspace_id: str,
    user_id: str,
    ws: WorkspaceContext = Depends(require_role("owner", "admin")),
):
    db = get_service_db()
    # Block removing the last owner
    owners = await asyncio.to_thread(
        lambda: db.table("workspace_members")
        .select("user_id")
        .eq("workspace_id", workspace_id)
        .eq("role", "owner")
        .execute()
    )
    if len(owners.data or []) <= 1:
        only_owner = (owners.data or [{}])[0].get("user_id")
        if only_owner == user_id:
            raise HTTPException(400, "Cannot remove the last owner of a workspace.")
    await asyncio.to_thread(
        lambda: db.table("workspace_members")
        .delete()
        .eq("workspace_id", workspace_id)
        .eq("user_id", user_id)
        .execute()
    )


# ── List members ───────────────────────────────────────────
@router.get("/{workspace_id}/members")
async def list_members(
    workspace_id: str,
    ws: WorkspaceContext = Depends(get_workspace),
):
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.table("workspace_members")
        .select("user_id, role, joined_at, profiles(email, full_name, avatar_url)")
        .eq("workspace_id", workspace_id)
        .execute()
    )
    return result.data or []
