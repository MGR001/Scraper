import asyncio
import logging

from fastapi import APIRouter, HTTPException

from ..database import get_db
from ..models.schemas import SourceCreate, SourceUpdate

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/")
async def list_sources():
    db = get_db()
    sources = await asyncio.to_thread(
        lambda: db.table("sources").select("*").order("created_at", desc=True).execute()
    )

    try:
        stats_result = await asyncio.to_thread(
            lambda: db.rpc("source_stats").execute()
        )
        stats_map: dict[str, dict] = {
            row["source_id"]: row for row in (stats_result.data or [])
        }
    except Exception:
        # source_stats() function not yet deployed — degrade gracefully
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
async def create_source(source: SourceCreate):
    db = get_db()
    try:
        result = await asyncio.to_thread(
            lambda: db.table("sources").insert(source.model_dump()).execute()
        )
        return result.data[0]
    except Exception as exc:
        if "unique" in str(exc).lower():
            raise HTTPException(400, "A source with this URL already exists.")
        raise HTTPException(500, str(exc))


@router.put("/{source_id}")
async def update_source(source_id: str, source: SourceUpdate):
    data = {k: v for k, v in source.model_dump().items() if v is not None}
    if not data:
        raise HTTPException(400, "No fields to update.")
    db = get_db()
    result = await asyncio.to_thread(
        lambda: db.table("sources").update(data).eq("id", source_id).execute()
    )
    if not result.data:
        raise HTTPException(404, "Source not found.")
    return result.data[0]


@router.delete("/{source_id}", status_code=204)
async def delete_source(source_id: str):
    db = get_db()
    await asyncio.to_thread(
        lambda: db.table("sources").delete().eq("id", source_id).execute()
    )


from pydantic import BaseModel as _PydanticBase


class UrlAdd(_PydanticBase):
    url: str


@router.post("/{source_id}/add-url")
async def add_url_to_source(source_id: str, body: UrlAdd):
    """Fetch a specific URL and embed its content into this source."""
    from ..services.scraper import fetch_page, extract_content, _store_content_chunks, validate_url

    try:
        validate_url(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    db = get_db()
    src = await asyncio.to_thread(
        lambda: db.table("sources").select("id, name").eq("id", source_id).execute()
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

    new_chunks = await _store_content_chunks(source_id, body.url, title, content)
    return {"new_chunks": new_chunks, "title": title, "url": body.url}
