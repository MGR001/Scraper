import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..auth import WorkspaceContext, get_workspace
from ..database import get_service_db
from ..models.schemas import ChatMessage
from ..rate_limit import check_rate_limit
from ..services.llm import chat_with_context, generate_source_summary, generate_comparison, generate_competitor_changes, generate_gtm_heatmap, generate_positioning_teardown, generate_campaign_messaging, generate_positioning_canvas, generate_feature_matrix, generate_kano_analysis

router = APIRouter()


async def _combine_own_company(db, workspace_id: str, build_content, max_chars: int = 2000) -> dict | None:
    """
    A workspace's own company can span multiple 'own'-category sources (main
    site, docs, blog, etc.) — combine content from ALL of them into one
    representative entry instead of only using the first source found.
    """
    own_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("workspace_id", workspace_id)
        .eq("is_active", True)
        .eq("category", "own")
        .execute()
    )
    own_sources = own_res.data or []
    if not own_sources:
        return None

    per_source_chars = max(400, max_chars // len(own_sources))
    parts = []
    for src in own_sources:
        chunk = await build_content(src, max_chars=per_source_chars)
        if chunk and chunk != "No content scraped yet.":
            parts.append(f"### {src['name']} ({src['url']})\n{chunk}")

    return {
        "name": own_sources[0]["name"],
        "url": own_sources[0]["url"],
        "content_summary": "\n\n".join(parts) or "No content scraped yet.",
    }


@router.post("/chat")
async def chat(message: ChatMessage, ws: WorkspaceContext = Depends(get_workspace)):
    """Ask a strategy question; GPT-4o answers using your scraped content."""
    check_rate_limit(ws.workspace_id, "chat")
    answer, sources = await chat_with_context(message.message, workspace_id=ws.workspace_id)
    return {"answer": answer, "sources": sources}


@router.post("/summary/{source_id}")
async def summarise_source(source_id: str, ws: WorkspaceContext = Depends(get_workspace)):
    """Generate (or regenerate) a competitive intelligence summary for a source."""
    db = get_service_db()

    src_res = await asyncio.to_thread(
        lambda: db.table("sources").select("*")
        .eq("id", source_id).eq("workspace_id", ws.workspace_id).execute()
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
async def competitive_comparison(ws: WorkspaceContext = Depends(get_workspace)):
    """Generate a McKinsey-style cross-competitor comparison from existing summaries."""
    check_rate_limit(ws.workspace_id, "comparison")
    db = get_service_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url, category, summary, summary_generated_at")
        .eq("workspace_id", ws.workspace_id)
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
async def competitor_changes(ws: WorkspaceContext = Depends(get_workspace)):
    """Compare the latest scrape session vs the one before it for each competitor."""
    db = get_service_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("workspace_id", ws.workspace_id)
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
async def gtm_heatmap(ws: WorkspaceContext = Depends(get_workspace)):
    check_rate_limit(ws.workspace_id, "gtm_heatmap")
    """Generate an ICP × competitor presence heatmap from scraped competitor content."""
    db = get_service_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("workspace_id", ws.workspace_id)
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
    own_company = await _combine_own_company(db, ws.workspace_id, _build_content)

    result = await generate_gtm_heatmap(competitors_data, own_company=own_company)
    # Flag own company competitor so frontend can highlight it
    if own_company:
        for c in result.get("competitors", []):
            if c.get("id") == "own-company" or c.get("name") == own_company["name"]:
                c["is_own"] = True
    return result


@router.get("/positioning-teardown")
async def positioning_teardown(ws: WorkspaceContext = Depends(get_workspace)):
    """Reconstruct each competitor's positioning into against / for / claim / proof."""
    check_rate_limit(ws.workspace_id, "positioning")
    db = get_service_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("workspace_id", ws.workspace_id)
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
    own_company = await _combine_own_company(db, ws.workspace_id, _build_content)

    return await generate_positioning_teardown(competitors_data, own_company=own_company)


@router.get("/positioning-canvas")
async def positioning_canvas(ws: WorkspaceContext = Depends(get_workspace)):
    """Plot competitors + own company on an AI-chosen 2-axis positioning canvas."""
    check_rate_limit(ws.workspace_id, "positioning_canvas")
    db = get_service_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("workspace_id", ws.workspace_id)
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

    own_company = await _combine_own_company(db, ws.workspace_id, _build_content)

    return await generate_positioning_canvas(competitors_data, own_company=own_company)


@router.get("/feature-matrix")
async def feature_matrix(ws: WorkspaceContext = Depends(get_workspace)):
    """Extract a canonical feature/claim list and mark each competitor's status against it."""
    check_rate_limit(ws.workspace_id, "feature_matrix")
    db = get_service_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("workspace_id", ws.workspace_id)
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

    own_company = await _combine_own_company(db, ws.workspace_id, _build_content)

    return await generate_feature_matrix(competitors_data, own_company=own_company)


@router.get("/kano-analysis")
async def kano_analysis(ws: WorkspaceContext = Depends(get_workspace)):
    """Classify product aspects across the market into Kano categories."""
    check_rate_limit(ws.workspace_id, "kano_analysis")
    db = get_service_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("workspace_id", ws.workspace_id)
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

    own_company = await _combine_own_company(db, ws.workspace_id, _build_content)

    return await generate_kano_analysis(competitors_data, own_company=own_company)


@router.get("/campaign-messaging")
async def campaign_messaging(ws: WorkspaceContext = Depends(get_workspace)):
    """Generate campaign messaging suggestions across five channels using competitor + own company intelligence."""
    check_rate_limit(ws.workspace_id, "messaging")
    db = get_service_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url")
        .eq("workspace_id", ws.workspace_id)
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

    own_company = await _combine_own_company(db, ws.workspace_id, _build_content)

    return await generate_campaign_messaging(competitors_data, own_company=own_company)
