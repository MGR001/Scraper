import asyncio

from fastapi import APIRouter, Depends

from ..auth import CurrentUser, WorkspaceContext, get_current_user, get_workspace
from ..config import settings
from ..database import get_service_db

router = APIRouter()


@router.get("/config")
async def public_config():
    """Return public (safe-to-expose) client-side config."""
    return {
        "supabase_url": settings.supabase_url,
        "supabase_anon_key": settings.supabase_anon_key,
    }


@router.get("/me")
async def get_me(
    user: CurrentUser = Depends(get_current_user),
):
    """Return the current user's profile and workspace memberships."""
    db = get_service_db()

    profile = await asyncio.to_thread(
        lambda: db.table("profiles").select("*").eq("id", user.id).execute()
    )
    memberships = await asyncio.to_thread(
        lambda: db.table("workspace_members")
        .select("workspace_id, role, workspaces(id, name, slug, company_name, onboarded_at)")
        .eq("user_id", user.id)
        .execute()
    )

    return {
        "user": profile.data[0] if profile.data else {"id": user.id, "email": user.email},
        "workspaces": [
            {
                "id": m["workspace_id"],
                "role": m["role"],
                "name": m.get("workspaces", {}).get("name", ""),
                "slug": m.get("workspaces", {}).get("slug", ""),
                "company_name": m.get("workspaces", {}).get("company_name", ""),
                "onboarded_at": m.get("workspaces", {}).get("onboarded_at"),
            }
            for m in (memberships.data or [])
        ],
    }


@router.patch("/me")
async def update_me(
    body: dict,
    user: CurrentUser = Depends(get_current_user),
):
    """Update the current user's profile (full_name, avatar_url)."""
    allowed = {k: v for k, v in body.items() if k in ("full_name", "avatar_url")}
    if not allowed:
        return {"ok": True}
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.table("profiles").update(allowed).eq("id", user.id).execute()
    )
    return result.data[0] if result.data else {}


@router.patch("/me")
async def update_me(
    body: dict,
    user: CurrentUser = Depends(get_current_user),
):
    """Update the current user's profile (full_name, avatar_url)."""
    allowed = {k: v for k, v in body.items() if k in ("full_name", "avatar_url")}
    if not allowed:
        return {"ok": True}
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.table("profiles").update(allowed).eq("id", user.id).execute()
    )
    return result.data[0] if result.data else {}
