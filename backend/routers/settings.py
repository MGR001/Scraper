import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser, WorkspaceContext, get_current_user, get_workspace, require_role
from ..database import get_service_db

router = APIRouter()


@router.get("/")
async def get_settings(
    user: CurrentUser = Depends(get_current_user),
    ws: WorkspaceContext = Depends(get_workspace),
):
    db = get_service_db()
    workspace = await asyncio.to_thread(
        lambda: db.table("workspaces").select("*").eq("id", ws.workspace_id).execute()
    )
    prefs = await asyncio.to_thread(
        lambda: db.table("user_preferences")
        .select("*")
        .eq("user_id", user.id)
        .eq("workspace_id", ws.workspace_id)
        .execute()
    )
    return {
        "workspace": workspace.data[0] if workspace.data else {},
        "preferences": prefs.data[0] if prefs.data else {},
        "role": ws.role,
    }


class WorkspaceSettings(BaseModel):
    name: str | None = None
    company_name: str | None = None
    company_url: str | None = None
    scrape_enabled: bool | None = None
    scrape_frequency: str | None = None
    scrape_hour: int | None = None
    timezone: str | None = None
    crawl_max_pages: int | None = None
    slack_webhook_url: str | None = None
    feature_matrix_categories: str | None = None


@router.patch("/workspace")
async def update_workspace_settings(
    body: WorkspaceSettings,
    ws: WorkspaceContext = Depends(require_role("owner", "admin")),
):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    if not data:
        return {}
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.table("workspaces").update(data).eq("id", ws.workspace_id).execute()
    )
    return result.data[0] if result.data else {}


class UserPreferences(BaseModel):
    email_digest: bool | None = None
    digest_frequency: str | None = None
    notify_pricing: bool | None = None
    notify_messaging: bool | None = None
    notify_sentiment: bool | None = None
    quiet_when_nothing: bool | None = None
    theme: str | None = None


@router.patch("/preferences")
async def update_preferences(
    body: UserPreferences,
    user: CurrentUser = Depends(get_current_user),
    ws: WorkspaceContext = Depends(get_workspace),
):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    db = get_service_db()
    await asyncio.to_thread(
        lambda: db.table("user_preferences").upsert(
            {"user_id": user.id, "workspace_id": ws.workspace_id, **data},
            on_conflict="user_id,workspace_id",
        ).execute()
    )
    return {"ok": True}


@router.post("/slack/test")
async def test_slack(
    ws: WorkspaceContext = Depends(require_role("owner", "admin")),
):
    db = get_service_db()
    workspace = await asyncio.to_thread(
        lambda: db.table("workspaces").select("slack_webhook_url").eq("id", ws.workspace_id).execute()
    )
    url = (workspace.data or [{}])[0].get("slack_webhook_url")
    if not url:
        raise HTTPException(400, "No Slack webhook configured.")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json={"text": "✓ RIvals Slack integration is working."})
    if resp.status_code != 200:
        raise HTTPException(502, f"Slack returned {resp.status_code}.")
    return {"ok": True}
