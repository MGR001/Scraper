import asyncio
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException

from ..database import get_db
from ..models.schemas import ChatMessage
from ..services.llm import chat_with_context, generate_source_summary, generate_comparison, generate_competitor_changes, generate_gtm_heatmap, generate_positioning_teardown

router = APIRouter()


@router.post("/chat")
async def chat(message: ChatMessage):
    """Ask a strategy question; GPT-4o answers using your scraped content."""
    answer, sources = await chat_with_context(message.message)
    return {"answer": answer, "sources": sources}


@router.post("/summary/{source_id}")
async def summarise_source(source_id: str):
    """Generate (or regenerate) a competitive intelligence summary for a source."""
    db = get_db()

    src_res = await asyncio.to_thread(
        lambda: db.table("sources").select("*").eq("id", source_id).execute()
    )
    if not src_res.data:
        raise HTTPException(404, "Source not found.")

    content_res = await asyncio.to_thread(
        lambda: db.table("scraped_content")
        .select("title, content, url")
        .eq("source_id", source_id)
        .order("scraped_at")
        .execute()
    )
    if not content_res.data:
        raise HTTPException(400, "No scraped content yet — scrape this source first.")

    summary = await generate_source_summary(src_res.data[0], content_res.data)

    await asyncio.to_thread(
        lambda: db.table("sources")
        .update({
            "summary": summary,
            "summary_generated_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("id", source_id)
        .execute()
    )
    return {"summary": summary}


@router.post("/comparison")
async def competitive_comparison():
    """Generate a McKinsey-style cross-competitor comparison from existing summaries."""
    db = get_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url, category, summary, summary_generated_at")
        .eq("is_active", True)
        .eq("category", "competitor")
        .execute()
    )

    sources_with_summaries = [s for s in (sources_res.data or []) if s.get("summary")]
    if len(sources_with_summaries) < 2:
        raise HTTPException(
            400,
            "At least 2 competitor sources with generated summaries are needed. "
            "Generate individual summaries first using \"Generate All\".",
        )

    return await generate_comparison(sources_with_summaries)


@router.get("/competitor-changes")
async def competitor_changes():
    """Compare the latest scrape session vs the one before it for each competitor."""
    db = get_db()
    session_window = timedelta(hours=2)  # scrapes within 2h of each other = same session

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("category", "competitor")
        .eq("is_active", True)
        .execute()
    )
    sources = sources_res.data or []
    if not sources:
        return {"results": [], "error": "No active competitor sources found."}

    results = []
    for src in sources:
        # Find the absolute latest scraped_at for this source
        latest_res = await asyncio.to_thread(
            lambda sid=src["id"]: db.table("scraped_content")
            .select("scraped_at")
            .eq("source_id", sid)
            .order("scraped_at", desc=True)
            .limit(1)
            .execute()
        )
        if not latest_res.data:
            results.append({
                "name": src["name"], "url": src["url"],
                "has_changes": False,
                "summary": "No scraped content found. Run a scrape first.",
                "changes": [], "stable": [],
                "latest_scrape": None, "previous_scrape": None,
            })
            continue

        latest_ts = datetime.fromisoformat(
            latest_res.data[0]["scraped_at"].replace("Z", "+00:00")
        )
        latest_session_start = (latest_ts - session_window).isoformat()

        # Fetch content from the latest session
        recent_res = await asyncio.to_thread(
            lambda sid=src["id"], s=latest_session_start: db.table("scraped_content")
            .select("content, scraped_at")
            .eq("source_id", sid)
            .gte("scraped_at", s)
            .order("scraped_at", desc=True)
            .limit(12)
            .execute()
        )
        recent_chunks = [r["content"] for r in (recent_res.data or []) if r.get("content")]

        # Find the latest row BEFORE the current session to anchor the previous session
        prev_anchor_res = await asyncio.to_thread(
            lambda sid=src["id"], s=latest_session_start: db.table("scraped_content")
            .select("scraped_at")
            .eq("source_id", sid)
            .lt("scraped_at", s)
            .order("scraped_at", desc=True)
            .limit(1)
            .execute()
        )

        old_chunks: list[str] = []
        prev_ts: datetime | None = None
        if prev_anchor_res.data:
            prev_ts = datetime.fromisoformat(
                prev_anchor_res.data[0]["scraped_at"].replace("Z", "+00:00")
            )
            prev_session_start = (prev_ts - session_window).isoformat()
            old_res = await asyncio.to_thread(
                lambda sid=src["id"], ps=prev_session_start, pe=latest_session_start:
                db.table("scraped_content")
                .select("content, scraped_at")
                .eq("source_id", sid)
                .gte("scraped_at", ps)
                .lt("scraped_at", pe)
                .order("scraped_at", desc=True)
                .limit(12)
                .execute()
            )
            old_chunks = [r["content"] for r in (old_res.data or []) if r.get("content")]

        change_data = await generate_competitor_changes(src["name"], old_chunks, recent_chunks)
        results.append({
            "name": src["name"],
            "url": src["url"],
            **change_data,
            "latest_scrape": latest_ts.isoformat(),
            "previous_scrape": prev_ts.isoformat() if prev_ts else None,
        })

    return {
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/gtm-heatmap")
async def gtm_heatmap():
    """Generate an ICP × competitor presence heatmap from scraped competitor content."""
    db = get_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("is_active", True)
        .eq("category", "competitor")
        .execute()
    )
    sources = sources_res.data or []
    if not sources:
        raise HTTPException(
            400,
            "No active competitor sources found. Add competitors in the Sources tab first.",
        )

    competitors_data = []
    for src in sources:
        content_res = await asyncio.to_thread(
            lambda sid=src["id"]: db.table("scraped_content")
            .select("content")
            .eq("source_id", sid)
            .order("scraped_at", desc=True)
            .limit(18)
            .execute()
        )
        rows = content_res.data or []
        seen: set[str] = set()
        parts: list[str] = []
        total = 0
        for row in rows:
            chunk = (row.get("content") or "").strip()
            if not chunk or chunk in seen:
                continue
            seen.add(chunk)
            parts.append(chunk[:400])
            total += len(chunk)
            if total >= 2400:
                break
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": "\n\n".join(parts) or "No content scraped yet.",
        })

    return await generate_gtm_heatmap(competitors_data)


@router.get("/positioning-teardown")
async def positioning_teardown():
    """Reconstruct each competitor's positioning into against / for / claim / proof."""
    db = get_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("is_active", True)
        .eq("category", "competitor")
        .execute()
    )
    sources = sources_res.data or []
    if not sources:
        raise HTTPException(400, "No active competitor sources found. Add competitors in the Sources tab first.")

    competitors_data = []
    for src in sources:
        content_res = await asyncio.to_thread(
            lambda sid=src["id"]: db.table("scraped_content")
            .select("content")
            .eq("source_id", sid)
            .order("scraped_at", desc=True)
            .limit(12)
            .execute()
        )
        rows = content_res.data or []
        seen: set[str] = set()
        parts: list[str] = []
        total = 0
        for row in rows:
            chunk = (row.get("content") or "").strip()
            if not chunk or chunk in seen:
                continue
            seen.add(chunk)
            parts.append(chunk[:400])
            total += len(chunk)
            if total >= 2000:
                break
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": "\n\n".join(parts) or "No content scraped yet.",
        })

    return await generate_positioning_teardown(competitors_data)
