import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from ..auth import WorkspaceContext, get_workspace
from ..database import get_service_db
from ..models.schemas import MentionsConfigUpdate, SourceCreate, SourceUpdate

router = APIRouter()
logger = logging.getLogger(__name__)


async def _paginated_rows(db, query_fn, page_size: int = 1000) -> list[dict]:
    """Loops a .range()-paged select until a partial page comes back. Never
    trust a single unbounded/loosely-bounded select on scraped_content --
    Supabase silently caps it at (by default) 1000 rows rather than
    erroring. query_fn(offset) must return the query with .range() applied."""
    rows: list[dict] = []
    offset = 0
    while True:
        res = await asyncio.to_thread(lambda o=offset: query_fn(o).execute())
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


async def _source_new_or_changed(db, source_id: str, cutoff: str) -> dict[str, int]:
    """new/changed counts for one source, given its latest session's start
    time. Both queries are scoped to one source and paginated -- a single
    crawl touching more than 1000 fresh pages, or a source with more than
    ~1000 fresh URLs' worth of query-string length, would otherwise either
    silently truncate (unbounded select) or 400 (an .in_() list that's too
    long for PostgREST's request size limit -- hit in testing on the
    sibling per-day version of this classification)."""
    fresh_rows = await _paginated_rows(
        db, lambda o: db.table("scraped_content").select("url")
        .eq("source_id", source_id).gte("scraped_at", cutoff)
        .range(o, o + 999)
    )
    fresh_urls = list({r["url"] for r in fresh_rows})
    if not fresh_urls:
        return {"new": 0, "changed": 0}

    prior_rows = await _paginated_rows(
        db, lambda o: db.table("scraped_content").select("url")
        .eq("source_id", source_id).lt("scraped_at", cutoff)
        .range(o, o + 999)
    )
    had_prior = {r["url"] for r in prior_rows}
    changed_n = sum(1 for u in fresh_urls if u in had_prior)
    return {"new": len(fresh_urls) - changed_n, "changed": changed_n}


async def _new_or_changed_counts(db, source_ids: list[str]) -> dict[str, dict[str, int]]:
    """Per source: {"new": n, "changed": m} from that source's latest
    finished crawl. "new" = a URL with zero older evidence (nothing about
    it existed before this crawl). "changed" = a URL that already had
    content before this crawl AND picked up at least one freshly-inserted
    chunk this time (scraped_at only changes on first insert, so a fresh
    chunk means real new content, not just a reconfirmation).

    One query per source (via _source_new_or_changed), not a single
    workspace-wide batch. An earlier version tried to batch this into 2-3
    queries total by bounding on the earliest cutoff across all sources --
    but with several active sources each producing hundreds to thousands
    of rows per crawl, that "small slice" was still routinely 1000+ rows,
    which is exactly Supabase's default unbounded-select cap. It silently
    truncated to an arbitrary subset and undercounted almost every source.
    Scoping each query to one source's one crawl keeps every request small
    regardless of workspace size, which matters more here than round-trip
    count."""
    if not source_ids:
        return {}
    try:
        sessions_res = await asyncio.to_thread(
            lambda: db.table("scrape_sessions").select("source_id, started_at")
            .in_("source_id", source_ids)
            .not_.is_("finished_at", "null")
            .order("started_at", desc=True)
            .execute()
        )
        latest_start: dict[str, str] = {}
        for row in (sessions_res.data or []):
            sid = row["source_id"]
            if sid not in latest_start:  # first hit per source_id = latest, rows are desc-ordered
                latest_start[sid] = row["started_at"]

        sids = list(latest_start.keys())
        results = await asyncio.gather(*(_source_new_or_changed(db, sid, latest_start[sid]) for sid in sids))
        return dict(zip(sids, results))
    except Exception:
        return {}


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

    new_changed_map = await _new_or_changed_counts(db, [s["id"] for s in sources.data])

    enriched = []
    for s in sources.data:
        sid = s["id"]
        row = stats_map.get(sid, {})
        s["pages_scraped"] = row.get("pages", 0)
        s["chunks_stored"] = row.get("chunks", 0)
        counts = new_changed_map.get(sid, {"new": 0, "changed": 0})
        s["new_pages"] = counts["new"]
        s["changed_pages"] = counts["changed"]
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


@router.patch("/{source_id}/mentions-config")
async def update_mentions_config(source_id: str, body: MentionsConfigUpdate,
                                 ws: WorkspaceContext = Depends(get_workspace)):
    """Set Reddit mention-monitoring config (terms, subreddits, enabled) for a
    competitor source."""
    data = {k: v for k, v in body.model_dump().items() if v is not None}
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

