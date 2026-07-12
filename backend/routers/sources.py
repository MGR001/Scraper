import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from ..auth import WorkspaceContext, get_workspace
from ..database import get_service_db
from ..models.schemas import SourceCreate, SourceUpdate

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/")
async def list_sources(ws: WorkspaceContext = Depends(get_workspace)):
    db = get_service_db()
    sources = await asyncio.to_thread(
        lambda: db.table("sources").select("*")
        .eq("workspace_id", ws.workspace_id)
        .order("created_at", desc=True).execute()
    )

    try:
        stats_result = await asyncio.to_thread(
            lambda: db.rpc("source_stats").execute()
        )
        stats_map: dict[str, dict] = {
            row["source_id"]: row for row in (stats_result.data or [])
        }
    except Exception:
        stats_map = {}

    enriched = []
    for s in sources.data:
        sid = s["id"]
        row = stats_map.get(sid, {})
        s["pages_scraped"] = row.get("pages", 0)
        s["chunks_stored"] = row.get("chunks", 0)
        enriched.append(s)

    return enriched


@router.post("/", status_code=201)
async def create_source(source: SourceCreate, ws: WorkspaceContext = Depends(get_workspace)):
    db = get_service_db()
    data = {**source.model_dump(), "workspace_id": ws.workspace_id}
    try:
        result = await asyncio.to_thread(
            lambda: db.table("sources").insert(data).execute()
        )
        return result.data[0]
    except Exception as exc:
        if "unique" in str(exc).lower():
            raise HTTPException(400, "A source with this URL already exists in this workspace.")
        raise HTTPException(500, str(exc))


@router.put("/{source_id}")
async def update_source(source_id: str, source: SourceUpdate, ws: WorkspaceContext = Depends(get_workspace)):
    data = {k: v for k, v in source.model_dump().items() if v is not None}
    if not data:
        raise HTTPException(400, "No fields to update.")
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.table("sources").update(data)
        .eq("id", source_id).eq("workspace_id", ws.workspace_id).execute()
    )
    if not result.data:
        raise HTTPException(404, "Source not found.")
    return result.data[0]


@router.delete("/{source_id}", status_code=204)
async def delete_source(source_id: str, ws: WorkspaceContext = Depends(get_workspace)):
    db = get_service_db()
    await asyncio.to_thread(
        lambda: db.table("sources").delete()
        .eq("id", source_id).eq("workspace_id", ws.workspace_id).execute()
    )


from pydantic import BaseModel as _PydanticBase


class UrlAdd(_PydanticBase):
    url: str


@router.post("/{source_id}/add-url")
async def add_url_to_source(source_id: str, body: UrlAdd, ws: WorkspaceContext = Depends(get_workspace)):
    """Fetch a specific URL and embed its content into this source."""
    from ..services.scraper import fetch_page, extract_content, _store_content_chunks, validate_url

    try:
        validate_url(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    db = get_service_db()
    src = await asyncio.to_thread(
        lambda: db.table("sources").select("id, name")
        .eq("id", source_id).eq("workspace_id", ws.workspace_id).execute()
    )
    if not src.data:
        raise HTTPException(404, "Source not found.")

    try:
        html = await fetch_page(body.url)
    except Exception as exc:
        raise HTTPException(400, f"Could not fetch URL: {exc}")

    title, content = extract_content(html)
    if not content.strip():
        raise HTTPException(400, "No readable content found at that URL.")

    new_chunks = await _store_content_chunks(source_id, body.url, title, content,
                                             workspace_id=ws.workspace_id)
    return {"new_chunks": new_chunks, "title": title, "url": body.url}

