import asyncio
import logging

from openai import AsyncOpenAI

from ..config import settings
from ..database import get_db
from .embeddings import get_embedding

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def get_relevant_context(question: str, top_k: int = 8) -> list[dict]:
    """Vector-search Supabase for the most relevant content chunks."""
    embedding = await get_embedding(question)
    db = get_db()
    try:
        result = await asyncio.to_thread(
            lambda: db.rpc(
                "match_content",
                {
                    "query_embedding": embedding,
                    "match_threshold": 0.1,
                    "match_count": top_k,
                },
            ).execute()
        )
        return result.data or []
    except Exception as exc:
        logger.error("Vector search failed: %s", exc)
        return []


async def chat_with_context(question: str) -> tuple[str, list[dict]]:
    """
    RAG pipeline: retrieve relevant chunks → build prompt → call GPT-4o.
    Returns (answer_text, list_of_source_refs).
    """
    sources = await get_relevant_context(question)

    if sources:
        context_parts = [
            f"[Source {i}] {s.get('title', 'Unknown')}\nURL: {s.get('url', '')}\n\n{s['content'][:800]}"
            for i, s in enumerate(sources, 1)
        ]
        context = "\n\n---\n\n".join(context_parts)
    else:
        context = "No relevant information found in the monitored websites."

    system_prompt = (
        "You are a strategic intelligence analyst. "
        "You answer questions about competitors, market trends, and business strategy "
        "based exclusively on the web content provided below. "
        "Be concise, analytical, and cite [Source N] references where relevant. "
        "If the context doesn't contain enough information to answer confidently, say so."
    )

    user_content = f"Context from monitored websites:\n\n{context}\n\n---\n\nQuestion: {question}"

    response = await _get_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.3,
        max_tokens=1500,
    )

    answer = response.choices[0].message.content or ""
    source_refs = [
        {
            "title": s.get("title", ""),
            "url": s.get("url", ""),
            "similarity": round(s.get("similarity", 0), 3),
        }
        for s in sources
    ]
    return answer, source_refs


async def generate_source_summary(source: dict, content_rows: list[dict]) -> str:
    """
    Generate a structured competitive intelligence summary for a source
    using all its scraped content.
    """
    # Build context from all chunks (cap total at ~6000 chars)
    seen: set[str] = set()
    parts: list[str] = []
    total = 0
    for row in content_rows:
        chunk = row["content"]
        if chunk in seen:
            continue
        seen.add(chunk)
        parts.append(chunk)
        total += len(chunk)
        if total >= 6000:
            break

    context = "\n\n---\n\n".join(parts)

    system_prompt = (
        "You are a strategic intelligence analyst. "
        "Based on the scraped website content below, write a concise competitive intelligence profile. "
        "Use this exact structure:\n\n"
        "**Overview** — What does this company/site do in 2–3 sentences.\n\n"
        "**Key Products or Services** — Bullet list of main offerings.\n\n"
        "**Target Market** — Who are their customers or audience.\n\n"
        "**Value Proposition** — What makes them stand out.\n\n"
        "**Notable Details** — Pricing, technology, team size, recent news, or anything strategically relevant.\n\n"
        "Be factual and grounded in the content provided. Do not speculate beyond what is present."
    )

    user_content = (
        f"Company/Source: {source.get('name', '')} ({source.get('url', '')})\n\n"
        f"Scraped content:\n\n{context}"
    )

    response = await _get_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        max_tokens=1000,
    )
    return response.choices[0].message.content or ""


async def generate_comparison(sources: list[dict]) -> dict:
    """
    Generate a comprehensive neutral competitive comparison matrix from source summaries.
    Covers all sources passed in; includes GTM, pricing, positioning, and key findings.
    """
    import json as _json

    parts = [
        f"## {s['name']} ({s['url']})\n\n{s['summary']}"
        for s in sources
        if s.get("summary")
    ]
    if not parts:
        return {}

    context = "\n\n---\n\n".join(parts)

    system_prompt = (
        "You are a neutral strategy analyst preparing an executive competitive intelligence briefing. "
        "Your tone is objective and factual — no bias, no winner. "
        "Analyze ALL competitor profiles provided and return ONLY valid JSON with no markdown fences or commentary. "
        "Include every competitor in the 'competitors' array. "
        "For fields you cannot determine from the content, use the string \"Unknown\". "
        "Infer GTM motion and pricing model from clues in the content (e.g. 'sign up free', 'contact sales', 'per seat'). "
        "Use this exact JSON schema:\n"
        "{\n"
        "  \"strategic_context\": \"<2-3 sentence neutral overview of the competitive landscape and key dynamics>\",\n"
        "  \"competitors\": [\n"
        "    {\n"
        "      \"name\": \"<exact company name>\",\n"
        "      \"positioning\": \"<how they position themselves in the market — one sentence>\",\n"
        "      \"target_market\": \"<primary customer segment in 6-10 words>\",\n"
        "      \"key_products\": [\"<product or service 1>\", \"<product or service 2>\", \"<product or service 3>\"],\n"
        "      \"pricing_model\": \"<one of: Free | Freemium | Subscription | Usage-Based | Per-Seat | Enterprise | Open-Source | Unknown>\",\n"
        "      \"pricing_detail\": \"<specific pricing tiers, ranges, or model details if available, else 'Not publicly disclosed'>\",\n"
        "      \"gtm_motion\": \"<one of: PLG | SLG | Channel | Community | Marketplace | Direct | Hybrid | Unknown>\",\n"
        "      \"gtm_channels\": [\"<channel 1>\", \"<channel 2>\"],\n"
        "      \"value_proposition\": \"<the core promise to the customer — one sentence>\",\n"
        "      \"key_differentiator\": \"<what genuinely sets this company apart from the others in this comparison — one sentence>\",\n"
        "      \"strengths\": [\"<observable strength 1>\", \"<observable strength 2>\"],\n"
        "      \"key_findings\": [\"<key finding 1>\", \"<key finding 2>\", \"<key finding 3>\"]\n"
        "    }\n"
        "  ],\n"
        "  \"uniqueness\": [\n"
        "    {\n"
        "      \"name\": \"<company name>\",\n"
        "      \"unique_angle\": \"<one sentence on what makes this competitor distinctly unique relative to all others in this set>\"\n"
        "    }\n"
        "  ],\n"
        "  \"strategic_implications\": [\n"
        "    \"<neutral strategic implication 1>\",\n"
        "    \"<neutral strategic implication 2>\",\n"
        "    \"<neutral strategic implication 3>\"\n"
        "  ]\n"
        "}"
    )

    response = await _get_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Competitor profiles to analyze (include ALL of them):\n\n{context}"},
        ],
        temperature=0.15,
        max_tokens=3500,
        response_format={"type": "json_object"},
    )
    return _json.loads(response.choices[0].message.content or "{}")


async def generate_competitor_changes(
    source_name: str, old_chunks: list[str], new_chunks: list[str]
) -> dict:
    """
    Compare old vs recent scraped content chunks for a competitor.
    Returns {has_changes, summary, changes, stable}.
    """
    import json as _json

    old_text = "\n\n---\n\n".join(old_chunks[:8])[:4000]
    new_text = "\n\n---\n\n".join(new_chunks[:8])[:4000]

    if not old_text:
        return {
            "has_changes": False,
            "summary": "No previous data to compare against — this may be a new source or was first scraped recently.",
            "changes": [],
            "stable": [],
        }

    system_prompt = (
        "You are a competitive intelligence analyst. "
        "Compare the PREVIOUS and RECENT scraped content from the same competitor website. "
        "Identify meaningful changes: new products or features, pricing changes, messaging shifts, "
        "new partnerships, leadership changes, or structural changes. "
        "Ignore minor cosmetic or navigation differences. "
        "Return ONLY valid JSON with this schema:\n"
        "{\n"
        "  \"has_changes\": <true|false>,\n"
        "  \"summary\": \"<2-3 sentence executive summary — what changed, or confirm no significant change>\",\n"
        "  \"changes\": [\"<specific meaningful change 1>\", \"<specific change 2>\"],\n"
        "  \"stable\": [\"<important area with no change>\"]\n"
        "}"
    )

    response = await _get_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"## {source_name}\n\n"
                    f"### PREVIOUS CONTENT (older scrape):\n{old_text}\n\n"
                    f"### RECENT CONTENT (latest scrape):\n{new_text}"
                ),
            },
        ],
        temperature=0.2,
        max_tokens=800,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    return _json.loads(raw)


async def generate_news_digest(articles: list[dict]) -> dict:
    """
    Generate an executive 5-day intelligence digest from a list of news articles.
    Returns structured JSON: headline, overview, themes (each with article links), strategic_takeaway.
    """
    import json as _json

    parts = []
    for i, a in enumerate(articles[:60], 1):
        parts.append(
            f"[{i}] {a.get('title', '(no title)')}\n"
            f"Source: {a.get('source_name', '')}\n"
            f"URL: {a.get('url', '')}\n"
            f"Snippet: {(a.get('snippet') or '')[:300]}"
        )
    context = "\n\n".join(parts)

    system_prompt = (
        "You are a senior strategy analyst. "
        "Based on the news articles below (collected over the last 5 days), "
        "write a concise executive intelligence digest. "
        "Return ONLY valid JSON matching this schema exactly:\n"
        "{\n"
        "  \"headline\": \"<one sentence capturing the single most important development>\",\n"
        "  \"overview\": \"<3-4 sentence executive summary of the key themes and developments>\",\n"
        "  \"themes\": [\n"
        "    {\n"
        "      \"theme\": \"<theme title in 4-6 words>\",\n"
        "      \"summary\": \"<2-3 sentence summary of this theme>\",\n"
        "      \"article_indices\": [<1-based indices of the most relevant articles>]\n"
        "    }\n"
        "  ],\n"
        "  \"strategic_takeaway\": \"<one forward-looking strategic implication>\"\n"
        "}\n\n"
        "Identify 3-5 themes. Be factual and grounded in the content provided. Do not speculate."
    )

    response = await _get_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"News articles from the last 5 days:\n\n{context}"},
        ],
        temperature=0.2,
        max_tokens=1500,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    return _json.loads(raw)


async def generate_gtm_heatmap(competitors: list[dict]) -> dict:
    """
    Given a list of {name, url, content_summary} dicts, produce an ICP × competitor
    heatmap with per-cell strength/trajectory/encroachment scores and defend/attack cards.
    """
    import json as _json

    parts = [
        f"## {c['name']} ({c['url']})\n{c['content_summary']}"
        for c in competitors
    ]
    context = "\n\n---\n\n".join(parts)

    system_prompt = (
        "You are a Go-to-Market strategist mapping a competitive landscape. "
        "Analyse the competitor website content below and:\n"
        "1. Identify 5-7 distinct customer segments / ICPs present in this market.\n"
        "2. Score each competitor in each segment.\n"
        "3. Produce Defend and Attack recommendations.\n"
        "Return ONLY valid JSON with no markdown fences. Use this exact schema:\n"
        "{\n"
        "  \"segments\": [\n"
        "    {\n"
        "      \"id\": \"<slug, e.g. seg1>\",\n"
        "      \"name\": \"<ICP name, 3-7 words>\",\n"
        "      \"description\": \"<one sentence>\",\n"
        "      \"status\": \"<safe|contested|at-risk>\"\n"
        "    }\n"
        "  ],\n"
        "  \"competitors\": [\n"
        "    { \"id\": \"<slugified-name>\", \"name\": \"<exact company name>\" }\n"
        "  ],\n"
        "  \"cells\": [\n"
        "    {\n"
        "      \"segment_id\": \"<seg id>\",\n"
        "      \"competitor_id\": \"<competitor id>\",\n"
        "      \"strength\": <0-4>,\n"
        "      \"trajectory\": \"<up|flat|down>\",\n"
        "      \"encroachment\": <0-2>\n"
        "    }\n"
        "  ],\n"
        "  \"defend\": {\n"
        "    \"headline\": \"<one-line threat summary>\",\n"
        "    \"segments\": [\"<segment name>\"],\n"
        "    \"rationale\": \"<2 sentences on why these need defending>\",\n"
        "    \"actions\": [\"<action 1>\", \"<action 2>\", \"<action 3>\"]\n"
        "  },\n"
        "  \"attack\": {\n"
        "    \"headline\": \"<one-line opportunity summary>\",\n"
        "    \"segments\": [\"<segment name>\"],\n"
        "    \"rationale\": \"<2 sentences on why these are attackable>\",\n"
        "    \"actions\": [\"<action 1>\", \"<action 2>\", \"<action 3>\"]\n"
        "  }\n"
        "}\n\n"
        "Strength: 0=no presence, 1=weak, 2=moderate, 3=strong, 4=dominant. "
        "Encroachment: 0=none, 1=actively targeting, 2=aggressively encroaching. "
        "Trajectory: up=growing momentum, flat=stable, down=declining. "
        "Produce one cell entry per segment × competitor combination."
    )

    response = await _get_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Competitor content:\n\n{context}"},
        ],
        temperature=0.2,
        max_tokens=2500,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return _json.loads(raw)
    except Exception:
        logger.error("GTM heatmap JSON parse failed: %s", raw[:200])
        return {}


async def generate_positioning_teardown(competitors: list[dict]) -> dict:
    """
    Reconstruct each competitor's positioning into four fixed fields:
    against, for, claim, proof. Also classify type: you | legacy | ai.
    Exactly one competitor receives type='you' (the dominant incumbent / category leader).
    """
    import json as _json

    parts = [
        f"## {c['name']} ({c['url']})\n{c['content_summary']}"
        for c in competitors
    ]
    context = "\n\n---\n\n".join(parts)

    system_prompt = (
        "You are a positioning strategist. Analyse the competitor website content and "
        "reconstruct each competitor's positioning into four fixed fields.\n"
        "Classify each as:\n"
        "  'you'    — the dominant category incumbent everyone else reacts against\n"
        "  'legacy' — established but not the dominant reference point\n"
        "  'ai'     — AI-native startup\n"
        "Assign exactly ONE competitor type='you'.\n"
        "Return ONLY valid JSON with no markdown fences. Schema:\n"
        "{\n"
        "  \"competitors\": [\n"
        "    {\n"
        "      \"name\": \"<exact name>\",\n"
        "      \"type\": \"<you|legacy|ai>\",\n"
        "      \"badge\": \"<short label: Incumbent | AI-native | PLG | Open-source | etc.>\",\n"
        "      \"against\": \"<who/what they position against — named rival or category>\",\n"
        "      \"for\": \"<target buyer / ICP in 5-8 words>\",\n"
        "      \"claim\": \"<core positioning claim — short, quotable, 8-15 words>\",\n"
        "      \"proof\": \"<evidence they cite: logos, corpus size, stats, process, funding>\"\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "If a field cannot be determined from the content, use null. Include ALL competitors."
    )

    response = await _get_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Competitor content:\n\n{context}"},
        ],
        temperature=0.2,
        max_tokens=1500,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return _json.loads(raw)
    except Exception:
        logger.error("Positioning teardown JSON parse failed: %s", raw[:200])
        return {}
