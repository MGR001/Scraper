"""
Authentication and workspace resolution for FastAPI endpoints.

Flow:
  1. Client sends  Authorization: Bearer <supabase_access_token>
  2. get_current_user() verifies the token via Supabase and returns user info.
  3. get_workspace() resolves which workspace the request targets (via
     X-Workspace-Id header or the user's sole membership) and checks membership.
  4. Routers declare both as dependencies; all DB calls use get_user_db(token)
     so RLS is enforced at the database level.
"""
import asyncio
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException
from supabase import Client

from .database import get_user_db, get_service_db


@dataclass
class CurrentUser:
    id: str
    email: str
    access_token: str


@dataclass
class WorkspaceContext:
    workspace_id: str
    role: str
    db: Client  # user-scoped client with RLS


async def get_current_user(
    authorization: Optional[str] = Header(None),
) -> CurrentUser:
    """Verify the Supabase JWT and return user info. Raises 401 on failure."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")
    token = authorization.split(" ", 1)[1]
    try:
        # Use the service client to validate the token (doesn't bypass RLS —
        # validation is just an auth call, not a data query).
        resp = await asyncio.to_thread(
            lambda: get_service_db().auth.get_user(token)
        )
        if not resp or not resp.user:
            raise HTTPException(status_code=401, detail="Invalid or expired token.")
        return CurrentUser(
            id=resp.user.id,
            email=resp.user.email or "",
            access_token=token,
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Token verification failed.")


async def get_workspace(
    user: CurrentUser = Depends(get_current_user),
    x_workspace_id: Optional[str] = Header(None),
) -> WorkspaceContext:
    """Resolve the workspace for this request and verify membership."""
    db = get_service_db()  # use service client to read membership (RLS not yet active)

    # Fetch all memberships for this user
    result = await asyncio.to_thread(
        lambda: db.table("workspace_members")
        .select("workspace_id, role")
        .eq("user_id", user.id)
        .execute()
    )
    memberships = result.data or []

    if not memberships:
        raise HTTPException(status_code=403, detail="You are not a member of any workspace.")

    if x_workspace_id:
        # Validate the requested workspace
        match = next((m for m in memberships if m["workspace_id"] == x_workspace_id), None)
        if not match:
            raise HTTPException(status_code=403, detail="Not a member of this workspace.")
        workspace_id = x_workspace_id
        role = match["role"]
    elif len(memberships) == 1:
        workspace_id = memberships[0]["workspace_id"]
        role = memberships[0]["role"]
    else:
        raise HTTPException(
            status_code=400,
            detail="You belong to multiple workspaces. Send X-Workspace-Id header.",
        )

    return WorkspaceContext(
        workspace_id=workspace_id,
        role=role,
        db=get_user_db(user.access_token),
    )


def require_role(*roles: str):
    """Dependency factory: raises 403 if the caller's role is not in *roles."""
    async def _check(ws: WorkspaceContext = Depends(get_workspace)) -> WorkspaceContext:
        if ws.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient role.")
        return ws
    return _check

