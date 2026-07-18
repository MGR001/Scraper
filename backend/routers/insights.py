import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..auth import WorkspaceContext, get_workspace
from ..database import get_service_db
from ..models.schemas import ChatMessage
from ..rate_limit import check_rate_limit
from ..services.embeddings import get_embedding
from ..services.llm import chat_with_context, generate_source_summary, generate_comparison, generate_competitor_changes, generate_gtm_heatmap, generate_positioning_teardown, generate_campaign_messaging, generate_positioning_canvas, generate_feature_matrix, generate_kano_analysis, generate_messaging_house, generate_battlecards

router = APIRouter()
logger = logging.getLogger(__name__)


_PAGE_TYPE_TIER = {"home": 0, "pricing": 0, "product": 1, "solutions": 1, "customers": 1}
_ALWAYS_KEEP_TYPES = {"home", "pricing"}

# Query embeddings are static per string — cache in-process rather than
# re-embedding the same short query on every framework call.
_query_embedding_cache: dict[str, list[float]] = {}


async def _cached_query_embedding(query: str) -> list[float]:
    if query not in _query_embedding_cache:
        _query_embedding_cache[query] = await get_embedding(query)
    return _query_embedding_cache[query]


async def _build_content_from_chunks(db, src: dict, limit: int = 15, max_chars: int = 8000) -> str:
    """Recency-ordered raw-chunk fallback, used only for sources that haven't
    been backfilled into page_summaries yet (see build_company_context)."""
    content_res = await asyncio.to_thread(
        lambda: db.table("scraped_content")
        .select("content, url")
        .eq("source_id", src["id"])
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
        chunk_url = row.get("url") or src["url"]
        parts.append(f"(Source URL: {chunk_url})\n{chunk[:800]}")
        total += len(chunk)
        if total >= max_chars:
            break
    return "\n\n".join(parts) or "No content scraped yet."


async def build_company_context(
    db, src: dict, page_types: list[str] | None = None, max_chars: int = 8000,
    selection_query: str | None = None,
) -> str:
    """
    Build a company's context block from its page_summaries: complete, typed,
    per-page summaries instead of arbitrary recency-ordered chunk fragments.
    Ordered home/pricing first, then product/solutions/customers, then the
    rest by recency. Each entry keeps the "(Source URL: url)\\nsummary" prefix
    format the framework prompts already instruct the model to cite as
    evidence links. Falls back to raw chunks if the source has no summaries
    yet (not backfilled) so nothing breaks mid-migration.

    For large sources where the filtered set still exceeds max_chars by more
    than 2x, selection_query (a short framework-specific query string) ranks
    summaries by semantic relevance via match_page_summaries and keeps the
    top ones that fit — home/pricing are always kept regardless of rank.
    """
    query = db.table("page_summaries").select("url, summary, page_type, updated_at").eq("source_id", src["id"])
    if page_types:
        query = query.in_("page_type", page_types)
    res = await asyncio.to_thread(lambda: query.execute())
    rows = res.data or []

    if page_types and len(rows) < 3:
        # Small sites won't have typed coverage for a narrow filter — widen to everything.
        all_res = await asyncio.to_thread(
            lambda: db.table("page_summaries")
            .select("url, summary, page_type, updated_at")
            .eq("source_id", src["id"])
            .execute()
        )
        rows = all_res.data or rows

    if not rows:
        return await _build_content_from_chunks(db, src, max_chars=max_chars)

    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    rows.sort(key=lambda r: _PAGE_TYPE_TIER.get(r.get("page_type"), 2))

    total_summary_chars = sum(len(r["summary"]) for r in rows)
    if selection_query and total_summary_chars > max_chars * 2:
        try:
            query_embedding = await _cached_query_embedding(selection_query)
            match_res = await asyncio.to_thread(
                lambda: db.rpc("match_page_summaries", {
                    "query_embedding": query_embedding,
                    "match_threshold": 0.0,
                    "match_count": max(len(rows), 50),
                    "p_source_id": src["id"],
                }).execute()
            )
            rank = {m["url"]: i for i, m in enumerate(match_res.data or [])}
            rows.sort(key=lambda r: (
                0 if r.get("page_type") in _ALWAYS_KEEP_TYPES else 1,
                rank.get(r["url"], len(rank)),
            ))
        except Exception as exc:
            logger.warning("Semantic summary selection failed for source %s, using recency order: %s",
                           src["id"], exc)

    parts: list[str] = []
    total = 0
    for r in rows:
        entry = f"(Source URL: {r['url']})\n{r['summary']}"
        if parts and total + len(entry) > max_chars:
            break
        parts.append(entry)
        total += len(entry)

    return "\n\n".join(parts) or "No content scraped yet."


async def _combine_own_company(
    db, workspace_id: str, page_types: list[str] | None = None, max_chars: int = 8000,
    selection_query: str | None = None,
) -> dict | None:
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

    per_source_chars = max(800, max_chars // len(own_sources))
    parts = []
    for src in own_sources:
        chunk = await build_company_context(
            db, src, page_types=page_types, max_chars=per_source_chars, selection_query=selection_query,
        )
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
    """Generate a McKinsey-style cross-competitor comparison from existing summaries, plus the own company from live content."""
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

    # Own company doesn't need a pre-generated summary like competitors do —
    # pull it from page_summaries (all types) so it's always included if present.
    own_company = await _combine_own_company(db, ws.workspace_id)

    result = await generate_comparison(sources_with_summaries, own_company=own_company)
    if own_company:
        for c in result.get("competitors", []):
            if c.get("name") == own_company["name"]:
                c["is_own_company"] = True
    return result


@router.get("/competitor-changes")
async def competitor_changes(ws: WorkspaceContext = Depends(get_workspace)):
    """Compare the latest scrape session vs the one before it for each competitor,
    plus the user's own company sources so they can track their own site's changes too."""
    db = get_service_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources")
        .select("id, name, url, category")
        .eq("workspace_id", ws.workspace_id)
        .in_("category", ["competitor", "own"])
        .eq("is_active", True)
        .execute()
    )
    sources = sources_res.data or []
    if not sources:
        return {"results": [], "error": "No active competitor or company sources found."}

    # Note: this own_company is only used to ground change-relevance commentary
    # below — the actual diffing (recent_chunks/old_chunks per source) stays on
    # raw scraped_content chunks, untouched by the page_summaries migration.
    own_company = await _combine_own_company(db, ws.workspace_id)

    results = []
    for src in sources:
        is_own = src["category"] == "own"

        # "current" is the latest finished scrape, "baseline" is the one
        # immediately before it — whatever the actual last two scrapes were,
        # not a fixed calendar window. A source scraped daily just shows
        # day-over-day changes; scrape it twice in a row with nothing new in
        # between and it correctly reports no changes.
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
                "name": src["name"], "url": src["url"], "is_own_company": is_own,
                "has_changes": False,
                "summary": "No scrape sessions found. Run a scrape first.",
                "changes": [], "stable": [],
                "latest_scrape": None, "previous_scrape": None,
            })
            continue

        latest_session = sessions[0]
        prev_session = sessions[1] if len(sessions) > 1 else None

        # Fetch up to 12 content chunks from the latest session, newest-first
        # by scraped_at (= first-seen time — unchanged when a chunk is just
        # reconfirmed by a later crawl). A source can have hundreds of stable
        # chunks that all get touched into the same session_id; without this
        # ordering, an unordered LIMIT 12 almost never lands on the handful
        # of genuinely new chunks, so brand-new pages silently never reach
        # the diff the LLM sees.
        recent_res = await asyncio.to_thread(
            lambda sess_id=latest_session["id"]: db.table("scraped_content")
            .select("content")
            .eq("session_id", sess_id)
            .order("scraped_at", desc=True)
            .limit(12)
            .execute()
        )
        recent_chunks = [r["content"] for r in (recent_res.data or []) if r.get("content")]

        # Baseline content: anything that already existed before this latest
        # crawl started. NOT queried by prev_session's session_id — every
        # unchanged chunk gets its session_id reassigned forward to the
        # newest crawl that reconfirmed it (see _store_content_chunks), so
        # after any new scrape almost nothing stays tagged with an older
        # session_id. scraped_at is set once on first insert and never
        # touched again, so it's the only reliable "did this exist before"
        # signal.
        old_chunks: list[str] = []
        if prev_session:
            old_res = await asyncio.to_thread(
                lambda sid=src["id"], cutoff=latest_session["started_at"]: db.table("scraped_content")
                .select("content")
                .eq("source_id", sid)
                .lt("scraped_at", cutoff)
                .order("scraped_at", desc=True)
                .limit(12)
                .execute()
            )
            old_chunks = [r["content"] for r in (old_res.data or []) if r.get("content")]

        # Confirmed brand-new pages (existed before the previous baseline
        # session, missing; exist now) get named to the LLM explicitly so
        # it interprets their actual content instead of either missing them
        # or — the old behaviour — us bolting on a bare "New page found: X"
        # bullet with no interpretation. Only computed when prev_session
        # exists, so a source's first-ever scrape (no baseline yet) never
        # reports changes — every page would otherwise look "new".
        new_page_titles: list[str] = []
        if prev_session:
            new_pages_res = await asyncio.to_thread(
                lambda sess_id=latest_session["id"], since=latest_session["started_at"]:
                    db.table("scraped_content")
                    .select("url, title")
                    .eq("session_id", sess_id)
                    .gte("scraped_at", since)  # scraped_at is set once, on first insert —
                                                # unchanged when a chunk is merely reconfirmed
                    .execute()
            )
            seen_urls: set[str] = set()
            for row in (new_pages_res.data or []):
                if row["url"] not in seen_urls:
                    seen_urls.add(row["url"])
                    new_page_titles.append(row.get("title") or row["url"])

        # For the own company's own source, there's no separate "own company" to
        # compare it against strategically — skip that framing and just report
        # what changed.
        change_data = await generate_competitor_changes(
            src["name"], old_chunks, recent_chunks,
            own_company=None if is_own else own_company,
            new_page_titles=new_page_titles,
        )
        if new_page_titles and old_chunks:
            # Belt-and-suspenders: a confirmed new page is a real change even
            # in the unlikely case the model's own has_changes still says no.
            # Gated on old_chunks being non-empty — if there's no real
            # baseline, generate_competitor_changes already correctly
            # reported "no previous data"; forcing has_changes true on top
            # of that would contradict its own summary text.
            change_data["has_changes"] = True

        results.append({
            "name": src["name"],
            "url": src["url"],
            "is_own_company": is_own,
            **change_data,
            "new_or_changed_pages": len(new_page_titles),
            "latest_scrape": latest_session["started_at"],
            "previous_scrape": prev_session["started_at"] if prev_session else None,
        })

    # Own company first, like the Competitors tab and Competitive Landscape Matrix.
    results.sort(key=lambda r: not r.get("is_own_company"))

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

    GTM_PAGE_TYPES = ["home", "solutions", "pricing", "customers"]
    GTM_QUERY = "target market segments, pricing model, customer types, sales motion"

    competitors_data = []
    for src in sources:
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": await build_company_context(db, src, page_types=GTM_PAGE_TYPES, selection_query=GTM_QUERY),
        })

    # Include own company as baseline reference if available
    own_company = await _combine_own_company(db, ws.workspace_id, page_types=GTM_PAGE_TYPES, selection_query=GTM_QUERY)

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

    POSITIONING_PAGE_TYPES = ["home", "about", "product", "solutions", "customers"]
    POSITIONING_QUERY = "positioning claims, target market, competitive differentiation"

    competitors_data = []
    for src in sources:
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": await build_company_context(
                db, src, page_types=POSITIONING_PAGE_TYPES, selection_query=POSITIONING_QUERY),
        })

    # Include own company as the reference baseline if available
    own_company = await _combine_own_company(
        db, ws.workspace_id, page_types=POSITIONING_PAGE_TYPES, selection_query=POSITIONING_QUERY)

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

    CANVAS_PAGE_TYPES = ["home", "about", "product", "solutions", "customers"]
    CANVAS_QUERY = "positioning claims, target market, competitive differentiation"

    competitors_data = []
    for src in sources:
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": await build_company_context(
                db, src, page_types=CANVAS_PAGE_TYPES, selection_query=CANVAS_QUERY),
        })

    own_company = await _combine_own_company(
        db, ws.workspace_id, page_types=CANVAS_PAGE_TYPES, selection_query=CANVAS_QUERY)

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

    # page_summaries already give one typed, complete summary per page (see
    # build_company_context) — feature/pricing claims live on product, solutions,
    # pricing and home pages, and each entry keeps its exact source URL for the
    # frontend's per-cell evidence hover.
    FEATURE_PAGE_TYPES = ["product", "solutions", "pricing", "home"]
    FEATURE_QUERY = "product features, capabilities, integrations, pricing tiers"

    competitors_data = []
    for src in sources:
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": await build_company_context(
                db, src, page_types=FEATURE_PAGE_TYPES, max_chars=8000, selection_query=FEATURE_QUERY),
        })

    own_company = await _combine_own_company(
        db, ws.workspace_id, page_types=FEATURE_PAGE_TYPES, max_chars=8000, selection_query=FEATURE_QUERY)

    ws_res = await asyncio.to_thread(
        lambda: db.table("workspaces").select("feature_matrix_categories").eq("id", ws.workspace_id).execute()
    )
    raw_categories = (ws_res.data or [{}])[0].get("feature_matrix_categories") or ""
    fixed_categories = [line.strip() for line in raw_categories.splitlines() if line.strip()] or None

    return await generate_feature_matrix(competitors_data, own_company=own_company, fixed_categories=fixed_categories)


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

    KANO_PAGE_TYPES = ["product", "solutions", "customers", "blog"]
    KANO_QUERY = "product features, customer needs, differentiators"

    competitors_data = []
    for src in sources:
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": await build_company_context(
                db, src, page_types=KANO_PAGE_TYPES, selection_query=KANO_QUERY),
        })

    own_company = await _combine_own_company(
        db, ws.workspace_id, page_types=KANO_PAGE_TYPES, selection_query=KANO_QUERY)

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

    CAMPAIGN_PAGE_TYPES = ["home", "pricing", "product", "blog"]
    CAMPAIGN_QUERY = "marketing messaging, pricing, product benefits, blog themes"

    competitors_data = []
    for src in sources:
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": await build_company_context(
                db, src, page_types=CAMPAIGN_PAGE_TYPES, selection_query=CAMPAIGN_QUERY),
        })

    own_company = await _combine_own_company(
        db, ws.workspace_id, page_types=CAMPAIGN_PAGE_TYPES, selection_query=CAMPAIGN_QUERY)

    return await generate_campaign_messaging(competitors_data, own_company=own_company)


@router.get("/messaging-house")
async def messaging_house(ws: WorkspaceContext = Depends(get_workspace)):
    """Build a messaging house (tagline, positioning statement, pillars) for the user's own company."""
    check_rate_limit(ws.workspace_id, "messaging_house")
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

    MESSAGING_HOUSE_PAGE_TYPES = ["home", "product", "solutions"]
    MESSAGING_HOUSE_QUERY = "brand positioning, tagline, value proposition, target audience"

    competitors_data = []
    for src in sources:
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": await build_company_context(
                db, src, page_types=MESSAGING_HOUSE_PAGE_TYPES, selection_query=MESSAGING_HOUSE_QUERY),
        })

    own_company = await _combine_own_company(
        db, ws.workspace_id, page_types=MESSAGING_HOUSE_PAGE_TYPES, selection_query=MESSAGING_HOUSE_QUERY)
    if not own_company:
        raise HTTPException(400, "Add your own company in the My Company tab first.")

    return await generate_messaging_house(competitors_data, own_company=own_company)


@router.get("/battlecards")
async def battlecards(ws: WorkspaceContext = Depends(get_workspace)):
    """Generate one sales battlecard per competitor, framed from the user's own company's perspective."""
    check_rate_limit(ws.workspace_id, "battlecards")
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

    BATTLECARD_PAGE_TYPES = ["pricing", "product", "customers", "home"]
    BATTLECARD_QUERY = "pricing tiers, product capabilities, customer proof"

    competitors_data = []
    for src in sources:
        competitors_data.append({
            "name": src["name"],
            "url": src["url"],
            "content_summary": await build_company_context(
                db, src, page_types=BATTLECARD_PAGE_TYPES, selection_query=BATTLECARD_QUERY),
        })

    own_company = await _combine_own_company(
        db, ws.workspace_id, page_types=BATTLECARD_PAGE_TYPES, selection_query=BATTLECARD_QUERY)
    if not own_company:
        raise HTTPException(400, "Add your own company in the My Company tab first.")

    result = await generate_battlecards(competitors_data, own_company=own_company)
    result["own_company_name"] = own_company["name"]
    return result
