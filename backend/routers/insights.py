import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..database import get_db
from ..models.schemas import ChatMessage
from ..services.llm import chat_with_context, generate_source_summary, generate_comparison, generate_competitor_changes, generate_gtm_heatmap, generate_positioning_teardown, generate_campaign_messaging

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
        # Get the two most recent *finished* scrape sessions for this source
        sessions_res = await asyncio.to_thread(
            lambda sid=src["id"]: db.table("scrape_sessions")
            .select("id, started_at, finished_at")
            .eq("source_id", sid)
            .not_.is_("finished_at", "null")
            .order("started_at", desc=True)
            .limit(2)
            .execute()
        )
        sessions = sessions_res.data or []

        if not sessions:
            results.append({
                "name": src["name"], "url": src["url"],
                "has_changes": False,
                "summary": "No scrape sessions found. Run a scrape first.",
                "changes": [], "stable": [],
                "latest_scrape": None, "previous_scrape": None,
            })
            continue

        latest_session = sessions[0]
        prev_session = sessions[1] if len(sessions) >= 2 else None

        # Fetch up to 12 content chunks from the latest session
        recent_res = await asyncio.to_thread(
            lambda sess_id=latest_session["id"]: db.table("scraped_content")
            .select("content")
            .eq("session_id", sess_id)
            .limit(12)
            .execute()
        )
        recent_chunks = [r["content"] for r in (recent_res.data or []) if r.get("content")]

        old_chunks: list[str] = []
        if prev_session:
            old_res = await asyncio.to_thread(
                lambda sess_id=prev_session["id"]: db.table("scraped_content")
                .select("content")
                .eq("session_id", sess_id)
                .limit(12)
                .execute()
            )
            old_chunks = [r["content"] for r in (old_res.data or []) if r.get("content")]

        change_data = await generate_competitor_changes(src["name"], old_chunks, recent_chunks)
        results.append({
            "name": src["name"],
            "url": src["url"],
            **change_data,
            "latest_scrape": latest_session["started_at"],
            "previous_scrape": prev_session["started_at"] if prev_session else None,
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

    async def _build_content(src: dict, limit: int = 18, max_chars: int = 2400) -> str:
        content_res = await asyncio.to_thread(
            lambda sid=src["id"]: db.table("scraped_content")
            .select("content")
            .eq("source_id", sid)
            .order("scraped_at", desc=True)
            .limit(limit)
            .execute()
        )
        seen: set[str] = set()
        parts: list[str] = []
        total = 0
        for row in (content_res.data or []):
            chunk = (row.get("content") or "").strip()
            if not chunk or chunk in seen:
                continue
            seen.add(chunk)
            parts.append(chunk[:400])
            total += len(chunk)
            if total >= max_chars:
                break
        return "\n\n".join(parts) or "No content scraped yet."

    competitors_data = []
    for src in sources:
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": await _build_content(src),
        })

    # Include own company as baseline reference if available
    own_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("is_active", True)
        .eq("category", "own")
        .execute()
    )
    own_company = None
    if own_res.data:
        src = own_res.data[0]
        own_company = {
            "name": src["name"],
            "url": src["url"],
            "content_summary": await _build_content(src),
        }

    result = await generate_gtm_heatmap(competitors_data, own_company=own_company)
    # Flag own company competitor so frontend can highlight it
    if own_company:
        for c in result.get("competitors", []):
            if c.get("id") == "own-company" or c.get("name") == own_company["name"]:
                c["is_own"] = True
    return result


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

    async def _build_content(src: dict, limit: int = 12, max_chars: int = 2000) -> str:
        content_res = await asyncio.to_thread(
            lambda sid=src["id"]: db.table("scraped_content")
            .select("content")
            .eq("source_id", sid)
            .order("scraped_at", desc=True)
            .limit(limit)
            .execute()
        )
        seen: set[str] = set()
        parts: list[str] = []
        total = 0
        for row in (content_res.data or []):
            chunk = (row.get("content") or "").strip()
            if not chunk or chunk in seen:
                continue
            seen.add(chunk)
            parts.append(chunk[:400])
            total += len(chunk)
            if total >= max_chars:
                break
        return "\n\n".join(parts) or "No content scraped yet."

    competitors_data = []
    for src in sources:
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": await _build_content(src),
        })

    # Include own company as the reference baseline if available
    own_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("is_active", True)
        .eq("category", "own")
        .execute()
    )
    own_company = None
    if own_res.data:
        src = own_res.data[0]
        own_company = {
            "name": src["name"],
            "url": src["url"],
            "content_summary": await _build_content(src),
        }

    return await generate_positioning_teardown(competitors_data, own_company=own_company)


@router.get("/campaign-messaging")
async def campaign_messaging():
    """Generate campaign messaging suggestions across five channels using competitor + own company intelligence."""
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

    async def _build_content(src: dict, limit: int = 12, max_chars: int = 2000) -> str:
        content_res = await asyncio.to_thread(
            lambda sid=src["id"]: db.table("scraped_content")
            .select("content")
            .eq("source_id", sid)
            .order("scraped_at", desc=True)
            .limit(limit)
            .execute()
        )
        seen: set[str] = set()
        parts: list[str] = []
        total = 0
        for row in (content_res.data or []):
            chunk = (row.get("content") or "").strip()
            if not chunk or chunk in seen:
                continue
            seen.add(chunk)
            parts.append(chunk[:400])
            total += len(chunk)
            if total >= max_chars:
                break
        return "\n\n".join(parts) or "No content scraped yet."

    competitors_data = []
    for src in sources:
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": await _build_content(src),
        })

    own_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("is_active", True)
        .eq("category", "own")
        .execute()
    )
    own_company = None
    if own_res.data:
        src = own_res.data[0]
        own_company = {
            "name": src["name"],
            "url": src["url"],
            "content_summary": await _build_content(src),
        }

    return await generate_campaign_messaging(competitors_data, own_company=own_company)
