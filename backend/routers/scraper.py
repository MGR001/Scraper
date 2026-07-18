import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from ..auth import WorkspaceContext, get_workspace
from ..database import get_service_db
from ..scrape_status import get_all_statuses
from ..services.scraper import scrape_source

router = APIRouter()
logger = logging.getLogger(__name__)


async def _get_max_pages(db, workspace_id: str) -> int:
    ws_res = await asyncio.to_thread(
        lambda: db.table("workspaces").select("crawl_max_pages").eq("id", workspace_id).execute()
    )
    return (ws_res.data or [{}])[0].get("crawl_max_pages") or 50


@router.post("/run/{source_id}")
async def run_scrape(source_id: str, background_tasks: BackgroundTasks,
                     ws: WorkspaceContext = Depends(get_workspace)):
    """Manually trigger a scrape for one source."""
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.table("sources").select("*")
        .eq("id", source_id).eq("workspace_id", ws.workspace_id).execute()
    )
    if not result.data:
        raise HTTPException(404, "Source not found.")
    source = result.data[0]

    from ..scrape_status import get_status
    status = get_status(source_id)
    if status.get("state") == "running":
        from datetime import datetime, timezone, timedelta
        updated = status.get("updated_at")
        stale = True
        if updated:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(updated)
            stale = age > timedelta(minutes=30)
        if not stale:
            raise HTTPException(409, "A scrape is already running for this source.")

    max_pages = await _get_max_pages(db, ws.workspace_id)
    background_tasks.add_task(scrape_source, source_id, source["url"],
                              max_pages=max_pages, workspace_id=ws.workspace_id,
                              crawl_scope=source.get("crawl_scope", "domain"),
                              sitemap_url=source.get("sitemap_url"))
    return {"message": f"Scrape started for '{source['name']}'. Checking sitemap…"}


@router.post("/run-all")
async def run_all_scrapes(background_tasks: BackgroundTasks,
                          ws: WorkspaceContext = Depends(get_workspace)):
    """Manually trigger scrapes for all active sources in this workspace."""
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.table("sources").select("*")
        .eq("workspace_id", ws.workspace_id).eq("is_active", True).execute()
    )
    max_pages = await _get_max_pages(db, ws.workspace_id)
    for source in result.data:
        background_tasks.add_task(scrape_source, source["id"], source["url"],
                                  max_pages=max_pages, workspace_id=ws.workspace_id,
                                  crawl_scope=source.get("crawl_scope", "domain"),
                                  sitemap_url=source.get("sitemap_url"))
    return {"message": f"Started scraping {len(result.data)} sources (sitemap-aware)."}


@router.get("/status")
async def scrape_statuses(ws: WorkspaceContext = Depends(get_workspace)):
    """Return current scrape state for all sources."""
    return get_all_statuses()


@router.get("/urls/{source_id}")
async def get_source_urls(source_id: str, ws: WorkspaceContext = Depends(get_workspace)):
    """List all distinct URLs scraped for a source."""
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.table("scraped_content")
        .select("url, title, scraped_at")
        .eq("source_id", source_id)
        .eq("workspace_id", ws.workspace_id)
        .order("scraped_at", desc=True)
        .execute()
    )
    seen: set[str] = set()
    urls = []
    for row in result.data:
        if row["url"] not in seen:
            seen.add(row["url"])
            urls.append({
                "url": row["url"],
                "title": row["title"],
                "scraped_at": row["scraped_at"],
            })
    return urls


@router.get("/content")
async def list_content(limit: int = 20, offset: int = 0, source_id: str | None = None,
                       ws: WorkspaceContext = Depends(get_workspace)):
    """Paginated list of scraped content (without full text)."""
    db = get_service_db()
    query = (
        db.table("scraped_content")
        .select("id, source_id, url, title, scraped_at, metadata")
        .eq("workspace_id", ws.workspace_id)
        .order("scraped_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if source_id:
        query = query.eq("source_id", source_id)
    result = await asyncio.to_thread(lambda: query.execute())
    return result.data


@router.get("/content/{content_id}")
async def get_content(content_id: str, ws: WorkspaceContext = Depends(get_workspace)):
    """Fetch full content for a single scraped item."""
    db = get_service_db()
    result = await asyncio.to_thread(
        lambda: db.table("scraped_content").select("*")
        .eq("id", content_id).eq("workspace_id", ws.workspace_id).execute()
    )
    if not result.data:
        raise HTTPException(404, "Content not found.")
    return result.data[0]


@router.get("/news/digest")
async def news_digest(ws: WorkspaceContext = Depends(get_workspace)):
    """AI executive digest of news-category content from the last 5 days."""
    from datetime import datetime, timezone, timedelta
    from ..services.llm import generate_news_digest
    from ..rate_limit import check_rate_limit
    check_rate_limit(ws.workspace_id, "news_digest")

    db = get_service_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, category")
        .eq("workspace_id", ws.workspace_id)
        .eq("category", "news")
        .eq("is_active", True)
        .execute()
    )
    sources = sources_res.data or []
    if not sources:
        return {"articles": [], "digest": None, "error": "No active news sources found."}

    articles: list[dict] = []
    for src in sources:
        result = await asyncio.to_thread(
            lambda sid=src["id"]: db.table("scraped_content")
            .select("id, source_id, url, title, scraped_at, content")
            .eq("source_id", sid)
            .gte("scraped_at", cutoff)
            .order("scraped_at", desc=True)
            .limit(100)
            .execute()
        )
        seen_urls: set[str] = set()
        for row in result.data or []:
            if row["url"] in seen_urls:
                continue
            seen_urls.add(row["url"])
            articles.append({
                "index": len(articles) + 1,
                "source_name": src["name"],
                "url": row["url"],
                "title": row["title"] or row["url"],
                "snippet": (row.get("content") or "")[:300],
                "scraped_at": row["scraped_at"],
            })

    articles.sort(key=lambda x: x["scraped_at"] or "", reverse=True)
    for i, a in enumerate(articles, 1):
        a["index"] = i

    if len(articles) < 3:
        return {
            "articles": articles,
            "digest": None,
            "error": f"Not enough recent content — only {len(articles)} article(s) in the last 5 days.",
        }

    digest = await generate_news_digest(articles)
    return {
        "articles": articles,
        "digest": digest,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/news")
async def news_feed(limit: int = 40, offset: int = 0, category: str | None = None,
                    source_ids: str | None = None,
                    ws: WorkspaceContext = Depends(get_workspace)):
    """Latest scraped pages — one entry per URL, fairly mixed across all sources.

    source_ids, if given, is a comma-separated list that restricts results to
    just those sources — used when the user has selected specific sources to
    filter by, so results aren't limited to whatever happened to be most
    recent across ALL sources before the filter was applied.
    """
    db = get_service_db()
    wanted_ids = set(source_ids.split(",")) if source_ids else None

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources").select("id, name, category")
        .eq("workspace_id", ws.workspace_id).execute()
    )
    sources = [
        s for s in (sources_res.data or [])
        if (not category or s.get("category") == category)
        and (not wanted_ids or s["id"] in wanted_ids)
    ]
    src_map = {s["id"]: s for s in sources}

    # Query per source so every source is represented regardless of scrape order.
    # Sequential calls to stay thread-safe with the Supabase sync client.
    all_items: list[dict] = []
    for src in sources:
        result = await asyncio.to_thread(
            lambda sid=src["id"]: db.table("scraped_content")
            .select("id, source_id, url, title, scraped_at, content, metadata")
            .eq("source_id", sid)
            .order("scraped_at", desc=True)
            .limit(200)
            .execute()
        )
        seen_urls: set[str] = set()
        for row in (result.data or []):
            if row["url"] in seen_urls:
                continue
            seen_urls.add(row["url"])
            all_items.append({
                "id": row["id"],
                "source_id": src["id"],
                "source_name": src.get("name", "Unknown"),
                "category": src.get("category", "general"),
                "url": row["url"],
                "title": row["title"],
                "snippet": (row.get("content") or "")[:300],
                "scraped_at": row["scraped_at"],
            })

    # Merge and sort by date desc, then paginate
    all_items.sort(key=lambda x: x["scraped_at"] or "", reverse=True)
    return all_items[offset: offset + limit]


@router.get("/stats")
async def get_stats(ws: WorkspaceContext = Depends(get_workspace)):
    """Overview statistics for the dashboard."""
    db = get_service_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources").select("id, is_active")
        .eq("workspace_id", ws.workspace_id).execute()
    )
    last_res = await asyncio.to_thread(
        lambda: db.table("scraped_content")
        .select("scraped_at")
        .eq("workspace_id", ws.workspace_id)
        .order("scraped_at", desc=True)
        .limit(1)
        .execute()
    )
    count_res = await asyncio.to_thread(
        lambda: db.table("scraped_content").select("id", count="exact")
        .eq("workspace_id", ws.workspace_id).execute()
    )

    sources = sources_res.data
    last_scrape = last_res.data[0]["scraped_at"] if last_res.data else None

    return {
        "total_sources": len(sources),
        "active_sources": sum(1 for s in sources if s["is_active"]),
        "total_chunks": count_res.count or 0,
        "last_scrape": last_scrape,
    }


async def _paginated_rows(db, query_fn, page_size: int = 1000) -> list[dict]:
    """Loops a .range()-paged select until a partial page comes back. Never
    trust a single unbounded/loosely-bounded select on scraped_content --
    Supabase silently caps it at (by default) 1000 rows rather than
    erroring, which quietly truncated an earlier version of this feature.
    query_fn(offset) must return the query with .range() already applied."""
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


async def _daily_new_changed(db, source_id: str, day_list: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    """Per-day counts of distinct URLs that were genuinely new (first ever
    seen that day) vs changed (already existed, picked up a fresh chunk
    that day) within day_list's window. Same new-vs-changed logic as
    _source_new_or_changed in sources.py, extended across every day in the
    window instead of just the latest crawl."""
    window_start = f"{day_list[0]}T00:00:00+00:00"

    in_window = await _paginated_rows(
        db, lambda o: db.table("scraped_content").select("url, scraped_at")
        .eq("source_id", source_id).gte("scraped_at", window_start)
        .range(o, o + 999)
    )
    if not in_window:
        return {}, {}

    by_url: dict[str, list[str]] = {}
    for row in in_window:
        by_url.setdefault(row["url"], []).append(row["scraped_at"])

    # Fetch prior evidence unfiltered-by-url and match in Python rather than
    # .in_("url", urls) -- a source with hundreds of fresh URLs can build an
    # IN-list long enough to blow past PostgREST's request size limit (seen
    # in testing: a plain 400 "Bad Request" with no useful detail).
    # Pagination already makes an unbounded fetch safe, so prefer it here.
    prior = await _paginated_rows(
        db, lambda o: db.table("scraped_content").select("url")
        .eq("source_id", source_id).lt("scraped_at", window_start)
        .range(o, o + 999)
    )
    had_prior_before_window = {r["url"] for r in prior}

    daily_new: dict[str, int] = {}
    daily_changed: dict[str, int] = {}
    for url, timestamps in by_url.items():
        dates = sorted({ts[:10] for ts in timestamps})
        if url in had_prior_before_window:
            for d in dates:
                daily_changed[d] = daily_changed.get(d, 0) + 1
        else:
            daily_new[dates[0]] = daily_new.get(dates[0], 0) + 1
            for d in dates[1:]:
                daily_changed[d] = daily_changed.get(d, 0) + 1

    return daily_new, daily_changed


@router.get("/daily-activity")
async def daily_activity(days: int = 21, ws: WorkspaceContext = Depends(get_workspace)):
    """Per-source daily timeline for the Dashboard's Activity tab: total
    pages scraped per day, plus that day's new/changed/unchanged split (for
    a stacked bar), plus each source's current new/changed page counts
    (same numbers as the Sources tab)."""
    from datetime import datetime, timedelta, timezone

    from .sources import _new_or_changed_counts

    db = get_service_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources").select("id, name, category")
        .eq("workspace_id", ws.workspace_id)
        .execute()
    )
    sources = sources_res.data or []
    if not sources:
        return {"days": [], "sources": []}

    source_ids = [s["id"] for s in sources]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    sessions_res = await asyncio.to_thread(
        lambda: db.table("scrape_sessions").select("source_id, started_at, pages")
        .in_("source_id", source_ids)
        .not_.is_("finished_at", "null")
        .gte("started_at", cutoff)
        .execute()
    )

    today = datetime.now(timezone.utc).date()
    day_list = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    day_index = {d: i for i, d in enumerate(day_list)}

    daily_map: dict[str, list[int]] = {sid: [0] * days for sid in source_ids}
    for row in (sessions_res.data or []):
        sid = row["source_id"]
        idx = day_index.get(row["started_at"][:10])
        if idx is not None:
            daily_map[sid][idx] += row.get("pages") or 0

    # These two are independent per source (and internally independent
    # across sources too) -- run them concurrently rather than blocking one
    # on the other, and gather every source's daily classification at once
    # instead of awaiting them one at a time. Cuts wall-clock time roughly
    # in proportion to source count, since this is I/O-bound.
    new_changed_map, per_source_daily = await asyncio.gather(
        _new_or_changed_counts(db, source_ids),
        asyncio.gather(*(_daily_new_changed(db, s["id"], day_list) for s in sources)),
    )

    result_sources = []
    for s, (daily_new_map, daily_changed_map) in zip(sources, per_source_daily):
        sid = s["id"]
        counts = new_changed_map.get(sid, {"new": 0, "changed": 0})
        daily_new = [daily_new_map.get(d, 0) for d in day_list]
        daily_changed = [daily_changed_map.get(d, 0) for d in day_list]
        daily_unchanged = [
            max(0, daily_map[sid][i] - daily_new[i] - daily_changed[i])
            for i in range(days)
        ]
        result_sources.append({
            "source_id": sid,
            "name": s["name"],
            "category": s["category"],
            "daily_pages": daily_map[sid],
            "daily_new": daily_new,
            "daily_changed": daily_changed,
            "daily_unchanged": daily_unchanged,
            "new_pages": counts["new"],
            "changed_pages": counts["changed"],
        })

    return {"days": day_list, "sources": result_sources}
