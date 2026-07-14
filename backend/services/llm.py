import asyncio
import logging

from openai import AsyncOpenAI

from ..config import settings
from ..database import get_db, get_service_db
from .embeddings import get_embedding

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def get_relevant_context(question: str, top_k: int = 8,
                               workspace_id: str | None = None) -> list[dict]:
    """Vector-search Supabase for the most relevant content chunks."""
    embedding = await get_embedding(question)
    db = get_service_db()
    params: dict = {
        "query_embedding": embedding,
        "match_threshold": settings.match_threshold,
        "match_count": top_k,
    }
    if workspace_id:
        params["p_workspace_id"] = workspace_id
    try:
        result = await asyncio.to_thread(
            lambda: db.rpc("match_content", params).execute()
        )
        return result.data or []
    except Exception as exc:
        logger.error("Vector search failed: %s", exc)
        return []


async def chat_with_context(question: str,
                            workspace_id: str | None = None) -> tuple[str, list[dict]]:
    """
    RAG pipeline: retrieve relevant chunks → build prompt → call GPT-4o.
    Returns (answer_text, list_of_source_refs).
    """
    sources = await get_relevant_context(question, workspace_id=workspace_id)

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


async def generate_gtm_heatmap(competitors: list[dict], own_company: dict | None = None) -> dict:
    """
    Given a list of {name, url, content_summary} dicts, produce an ICP × competitor
    heatmap with per-cell strength/trajectory/encroachment scores and defend/attack cards.
    If own_company is provided it is included as the first column so the user can see
    their own segment coverage vs the competitive landscape.
    """
    import json as _json

    all_entries = ([own_company] if own_company else []) + competitors
    parts = [
        f"## {'YOUR COMPANY: ' if own_company and c is own_company else ''}{c['name']} ({c['url']})\n{c['content_summary']}"
        for c in all_entries
    ]
    context = "\n\n---\n\n".join(parts)

    own_note = (
        f"The first company ('YOUR COMPANY: {own_company['name']}') is the user's own company. "
        "Include it as the first entry in the competitors array with id='own-company'. "
        "The Defend and Attack recommendations should be written from that company's perspective. "
        if own_company else ""
    )

    system_prompt = (
        "You are a Go-to-Market strategist mapping a competitive landscape. "
        f"{own_note}"
        "Analyse the website content below and:\n"
        "1. Identify 5-7 distinct customer segments / ICPs present in this market.\n"
        "2. Score each company in each segment.\n"
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


async def generate_positioning_teardown(competitors: list[dict], own_company: dict | None = None) -> dict:
    """
    Reconstruct each competitor's positioning into four fixed fields:
    against, for, claim, proof. Also classify type: you | legacy | ai.
    If own_company is provided it is included as the first card with type='you'.
    """
    import json as _json

    all_entries = ([own_company] if own_company else []) + competitors
    parts = [
        f"## {'YOUR COMPANY: ' if own_company and c is own_company else ''}{c['name']} ({c['url']})\n{c['content_summary']}"
        for c in all_entries
    ]
    context = "\n\n---\n\n".join(parts)

    own_note = (
        f"The first company ('YOUR COMPANY: {own_company['name']}') is the user's own company — assign it type='you'. "
        "For each competitor, frame the 'against' field in terms of how they position relative to "
        f"{own_company['name']} and the competitive landscape. "
        if own_company else
        "Assign exactly ONE competitor type='you' (the dominant category incumbent everyone else reacts against).\n"
    )

    system_prompt = (
        "You are a positioning strategist. Analyse the website content and "
        "reconstruct each company's positioning into four fixed fields.\n"
        + own_note +
        "Classify each company as:\n"
        "  'you'    — the user's own company (or dominant incumbent if none provided)\n"
        "  'legacy' — established but not the dominant reference point\n"
        "  'ai'     — AI-native startup\n"
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


async def generate_campaign_messaging(competitors: list[dict], own_company: dict | None = None) -> dict:
    """
    Using competitive intelligence, generate campaign messaging suggestions across
    five channels: Paid Social, Email Nurture, Content/SEO, Sales Outreach, PR.
    If own_company is provided, messages emphasise the user's differentiators.
    """
    import json as _json

    all_entries = ([own_company] if own_company else []) + competitors
    parts = [
        f"## {'YOUR COMPANY: ' if own_company and c is own_company else ''}{c['name']} ({c['url']})\n{c['content_summary']}"
        for c in all_entries
    ]
    context = "\n\n---\n\n".join(parts)

    own_note = (
        f"The first entry ('YOUR COMPANY: {own_company['name']}') is the user's own company. "
        "Write all messaging from that company's perspective, exploiting gaps competitors leave open "
        f"and differentiating clearly from them. Refer to the company as '{own_company['name']}'.\n"
        if own_company else
        "Write messaging from the perspective of a challenger brand entering this market.\n"
    )

    system_prompt = (
        "You are a senior B2B campaign strategist. Using the competitive intelligence below, "
        "generate campaign messaging suggestions across five channels.\n"
        + own_note +
        "For each channel produce exactly 3 message variants, each targeting a distinct ICP or angle.\n"
        "Rules:\n"
        "  - Headlines must be punchy and specific (max 10 words)\n"
        "  - Body copy is 2-3 sentences — no buzzwords, no 'leverage', no 'synergy'\n"
        "  - Each angle must exploit a real gap or weakness visible in the competitor content\n"
        "  - CTA should be action-oriented (max 6 words)\n"
        "Return ONLY valid JSON with no markdown fences. Schema:\n"
        "{\n"
        "  \"strategic_summary\": \"<2-3 sentences on the overall messaging opportunity>\",\n"
        "  \"channels\": [\n"
        "    {\n"
        "      \"id\": \"<slug, e.g. paid-social>\",\n"
        "      \"name\": \"<channel name>\",\n"
        "      \"messages\": [\n"
        "        {\n"
        "          \"icp\": \"<target buyer persona in 5-8 words>\",\n"
        "          \"angle\": \"<competitive angle or gap being exploited, 5-10 words>\",\n"
        "          \"headline\": \"<attention-grabbing headline>\",\n"
        "          \"body\": \"<message body copy, 2-3 sentences>\",\n"
        "          \"cta\": \"<call to action>\"\n"
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Channels (use these ids and names exactly): "
        "paid-social / Paid Social, "
        "email-nurture / Email Nurture, "
        "content-seo / Content & SEO, "
        "sales-outreach / Sales Outreach, "
        "pr-thought-leadership / PR & Thought Leadership."
    )

    response = await _get_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Competitive intelligence:\n\n{context}"},
        ],
        temperature=0.35,
        max_tokens=3000,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return _json.loads(raw)
    except Exception:
        logger.error("Campaign messaging JSON parse failed: %s", raw[:200])
        return {}


async def generate_positioning_canvas(competitors: list[dict], own_company: dict | None = None) -> dict:
    """
    Plot each company on a 2-axis positioning canvas. The model chooses whichever
    two dimensions are most differentiating for this specific market based on the
    scraped content, then places each company (0-100 on each axis) with a rationale.
    """
    import json as _json

    all_entries = ([own_company] if own_company else []) + competitors
    parts = [
        f"## {'YOUR COMPANY: ' if own_company and c is own_company else ''}{c['name']} ({c['url']})\n{c['content_summary']}"
        for c in all_entries
    ]
    context = "\n\n---\n\n".join(parts)

    own_note = (
        f"The first company ('YOUR COMPANY: {own_company['name']}') is the user's own company — "
        "set is_own=true for it only.\n"
        if own_company else ""
    )

    system_prompt = (
        "You are a positioning strategist building a 2x2 positioning canvas (a classic strategy "
        "quadrant map). " + own_note +
        "Analyse the website content below and:\n"
        "1. Choose the TWO dimensions that most differentiate these companies from each other "
        "(e.g. 'Price' vs 'Ease of setup', 'Breadth of platform' vs 'Depth of specialization', "
        "'Self-serve' vs 'Enterprise / high-touch' — pick whatever is genuinely most revealing for "
        "THIS market rather than defaulting to generic axes).\n"
        "2. For each axis, give a short label and what the low and high ends mean.\n"
        "3. Place every company on both axes using a 0-100 scale, with a one-sentence rationale "
        "grounded in the content.\n"
        "Return ONLY valid JSON with no markdown fences. Schema:\n"
        "{\n"
        "  \"x_axis\": { \"label\": \"<axis name>\", \"low\": \"<what 0 means>\", \"high\": \"<what 100 means>\" },\n"
        "  \"y_axis\": { \"label\": \"<axis name>\", \"low\": \"<what 0 means>\", \"high\": \"<what 100 means>\" },\n"
        "  \"companies\": [\n"
        "    {\n"
        "      \"name\": \"<exact company name>\",\n"
        "      \"is_own\": <true|false>,\n"
        "      \"x\": <0-100>,\n"
        "      \"y\": <0-100>,\n"
        "      \"rationale\": \"<one sentence grounding the placement in the content>\"\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Include every company provided. Base placements on evidence in the content, not assumptions."
    )

    response = await _get_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Competitor content:\n\n{context}"},
        ],
        temperature=0.25,
        max_tokens=1500,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return _json.loads(raw)
    except Exception:
        logger.error("Positioning canvas JSON parse failed: %s", raw[:200])
        return {}


async def generate_feature_matrix(competitors: list[dict], own_company: dict | None = None) -> dict:
    """
    Extract a canonical list of features/claims mentioned across the companies'
    content, then mark each company's status (yes/partial/no) for each one.
    """
    import json as _json

    all_entries = ([own_company] if own_company else []) + competitors
    parts = [
        f"## {'YOUR COMPANY: ' if own_company and c is own_company else ''}{c['name']} ({c['url']})\n{c['content_summary']}"
        for c in all_entries
    ]
    context = "\n\n---\n\n".join(parts)
    company_names = [c["name"] for c in all_entries]

    system_prompt = (
        "You are a competitive analyst building a feature/claim comparison matrix. "
        "Analyse the website content below and:\n"
        "1. Identify 8-14 distinct features or claims that appear across these companies "
        "(product capabilities, integrations, guarantees, certifications, pricing-model traits, "
        "etc.) — only include ones that are genuinely comparable across multiple companies.\n"
        "2. For every feature, mark each company's status:\n"
        "   'yes'     — clearly claimed/offered\n"
        "   'partial' — offered in a limited form, or implied but not explicit\n"
        "   'no'      — not mentioned / not offered\n"
        "3. Where status is 'yes' or 'partial', include a short supporting quote or paraphrase as "
        "evidence.\n"
        "Return ONLY valid JSON with no markdown fences. Schema:\n"
        "{\n"
        "  \"features\": [ \"<feature name, short>\" ],\n"
        "  \"companies\": [ \"<exact company name, in the order given>\" ],\n"
        "  \"cells\": [\n"
        "    {\n"
        "      \"feature\": \"<feature name — must match one in features[]>\",\n"
        "      \"company\": \"<company name — must match one in companies[]>\",\n"
        "      \"status\": \"<yes|partial|no>\",\n"
        "      \"evidence\": \"<short quote/paraphrase, or null if status is 'no'>\"\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        f"Companies, in order: {', '.join(company_names)}. "
        "Produce one cell entry for every feature x company combination."
    )

    response = await _get_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Competitor content:\n\n{context}"},
        ],
        temperature=0.2,
        max_tokens=3000,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return _json.loads(raw)
    except Exception:
        logger.error("Feature matrix JSON parse failed: %s", raw[:200])
        return {}


async def generate_kano_analysis(competitors: list[dict], own_company: dict | None = None) -> dict:
    """
    Classify product aspects found across the market into Kano categories:
    must-be (baseline expected), performance (more-is-better), delighter (exciter).
    """
    import json as _json

    all_entries = ([own_company] if own_company else []) + competitors
    parts = [
        f"## {'YOUR COMPANY: ' if own_company and c is own_company else ''}{c['name']} ({c['url']})\n{c['content_summary']}"
        for c in all_entries
    ]
    context = "\n\n---\n\n".join(parts)

    system_prompt = (
        "You are a product strategist applying the Kano model to a competitive market. "
        "Analyse the website content below and:\n"
        "1. Identify 8-14 distinct product aspects/features present across these companies.\n"
        "2. Classify each into exactly one Kano category:\n"
        "   'must-be'     — baseline/expected; nearly every company offers it; its absence would "
        "be disqualifying\n"
        "   'performance' — more-is-better; companies differentiate on how much/how well they do it\n"
        "   'delighter'   — an exciter; rare, unexpected, offered by few or one company; not "
        "expected by buyers\n"
        "3. For each aspect, list which companies (exact names) offer/emphasise it, and a "
        "one-sentence rationale for the classification.\n"
        "Return ONLY valid JSON with no markdown fences. Schema:\n"
        "{\n"
        "  \"aspects\": [\n"
        "    {\n"
        "      \"name\": \"<aspect/feature name, short>\",\n"
        "      \"category\": \"<must-be|performance|delighter>\",\n"
        "      \"rationale\": \"<one sentence grounding the classification in the content>\",\n"
        "      \"offered_by\": [ \"<company name>\" ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Base classification on how the aspect actually appears across THIS set of companies "
        "(e.g. something every company has is 'must-be' regardless of how impressive it sounds), "
        "not generic assumptions."
    )

    response = await _get_client().chat.completions.create(
        model=settings.chat_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Competitor content:\n\n{context}"},
        ],
        temperature=0.25,
        max_tokens=2500,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return _json.loads(raw)
    except Exception:
        logger.error("Kano analysis JSON parse failed: %s", raw[:200])
        return {}
