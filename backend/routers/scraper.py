import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException

from ..database import get_db
from ..scrape_status import get_all_statuses
from ..services.scraper import scrape_source

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/run/{source_id}")
async def run_scrape(source_id: str, background_tasks: BackgroundTasks):
    """Manually trigger a scrape for one source."""
    db = get_db()
    result = await asyncio.to_thread(
        lambda: db.table("sources").select("*").eq("id", source_id).execute()
    )
    if not result.data:
        raise HTTPException(404, "Source not found.")
    source = result.data[0]
    background_tasks.add_task(scrape_source, source_id, source["url"])
    return {"message": f"Scrape started for '{source['name']}'. Checking sitemap…"}


@router.post("/run-all")
async def run_all_scrapes(background_tasks: BackgroundTasks):
    """Manually trigger scrapes for all active sources."""
    db = get_db()
    result = await asyncio.to_thread(
        lambda: db.table("sources").select("*").eq("is_active", True).execute()
    )
    for source in result.data:
        background_tasks.add_task(scrape_source, source["id"], source["url"])
    return {"message": f"Started scraping {len(result.data)} sources (sitemap-aware)."}


@router.get("/status")
async def scrape_statuses():
    """Return current scrape state for all sources."""
    return get_all_statuses()


@router.get("/urls/{source_id}")
async def get_source_urls(source_id: str):
    """List all distinct URLs scraped for a source."""
    db = get_db()
    result = await asyncio.to_thread(
        lambda: db.table("scraped_content")
        .select("url, title, scraped_at")
        .eq("source_id", source_id)
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
async def list_content(limit: int = 20, offset: int = 0, source_id: str | None = None):
    """Paginated list of scraped content (without full text)."""
    db = get_db()
    query = (
        db.table("scraped_content")
        .select("id, source_id, url, title, scraped_at, metadata")
        .order("scraped_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if source_id:
        query = query.eq("source_id", source_id)
    result = await asyncio.to_thread(lambda: query.execute())
    return result.data


@router.get("/content/{content_id}")
async def get_content(content_id: str):
    """Fetch full content for a single scraped item."""
    db = get_db()
    result = await asyncio.to_thread(
        lambda: db.table("scraped_content").select("*").eq("id", content_id).execute()
    )
    if not result.data:
        raise HTTPException(404, "Content not found.")
    return result.data[0]


@router.get("/news/digest")
async def news_digest():
    """AI executive digest of news-category content from the last 5 days."""
    from datetime import datetime, timezone, timedelta
    from ..services.llm import generate_news_digest

    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, category")
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
async def news_feed(limit: int = 40, offset: int = 0, category: str | None = None):
    """Latest scraped pages — one entry per URL, fairly mixed across all sources."""
    db = get_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources").select("id, name, category").execute()
    )
    sources = [
        s for s in (sources_res.data or [])
        if not category or s.get("category") == category
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
async def get_stats():
    """Overview statistics for the dashboard."""
    db = get_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources").select("id, is_active").execute()
    )
    last_res = await asyncio.to_thread(
        lambda: db.table("scraped_content")
        .select("scraped_at")
        .order("scraped_at", desc=True)
        .limit(1)
        .execute()
    )
    count_res = await asyncio.to_thread(
        lambda: db.table("scraped_content").select("id", count="exact").execute()
    )

    sources = sources_res.data
    last_scrape = last_res.data[0]["scraped_at"] if last_res.data else None

    return {
        "total_sources": len(sources),
        "active_sources": sum(1 for s in sources if s["is_active"]),
        "total_chunks": count_res.count or 0,
        "last_scrape": last_scrape,
    }
