import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from ..config import settings
from ..database import get_service_db
from .embeddings import get_embedding
from .llm import _create_completion

logger = logging.getLogger(__name__)

_VALID_PAGE_TYPES = {
    "home", "pricing", "product", "solutions", "customers",
    "blog", "news", "about", "careers", "legal", "other",
}

_SYSTEM_PROMPT = (
    "You are extracting competitive intelligence from a single web page.\n"
    "Return ONLY valid JSON: {\"page_type\": \"...\", \"summary\": \"...\"}\n\n"
    "page_type: exactly one of home, pricing, product, solutions, customers,\n"
    "blog, news, about, careers, legal, other. Judge from URL and content.\n\n"
    "summary: dense, telegraphic, max 120 words. Extract ONLY facts present\n"
    "on the page:\n"
    "- what is claimed or offered\n"
    "- any prices, tiers, or pricing-model statements (always include if present)\n"
    "- named features, products, integrations, certifications\n"
    "- named customers, partners, or case-study subjects\n"
    "- stated target audience\n"
    "No marketing adjectives, no filler, no inference beyond the page.\n"
    "If the page is boilerplate (cookie/legal/nav shell), summary may be one line."
)


def _cap_text(text: str, max_chars: int = 12000) -> str:
    """Head + tail truncation — intros and pricing tables both carry signal,
    the truncated middle of a long page rarely does."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n...\n" + text[-half:]


def _parse_summary_response(raw: str) -> dict:
    """Strip code fences if present; never raises — falls back to page_type='other'
    plus the raw (truncated) text on any parse failure."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
        page_type = data.get("page_type", "other")
        if page_type not in _VALID_PAGE_TYPES:
            page_type = "other"
        summary = (data.get("summary") or "").strip()
        if not summary:
            summary = text[:500]
        return {"page_type": page_type, "summary": summary}
    except Exception as exc:
        logger.warning("Failed to parse page summary JSON: %s", exc)
        return {"page_type": "other", "summary": text[:500] or "(summary unavailable)"}


async def summarize_page(url: str, title: str, chunks: list[str]) -> dict:
    """Returns {"summary": str, "page_type": str}."""
    combined = _cap_text("\n\n".join(chunks))
    user_content = f"URL: {url}\nTitle: {title or '(no title)'}\n\nContent:\n{combined}"

    try:
        response = await _create_completion(
            model=settings.summary_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            max_completion_tokens=500,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
    except Exception as exc:
        logger.error("Page summarization failed for %s: %s", url, exc)
        return {"page_type": "other", "summary": "(summary unavailable — model call failed)"}

    return _parse_summary_response(raw)


async def store_page_summary(
    workspace_id: str, source_id: str, url: str, title: str,
    chunks: list[str], session_id: str | None = None,
) -> bool:
    """Upserts a page_summaries row for (source_id, url). Skips the LLM call
    entirely when the page's content hasn't changed since the last summary.
    Returns True if a new summary was generated, False if skipped (unchanged
    or no content) — callers use this for sweep-level cost visibility."""
    if not chunks:
        return False

    content_hash = hashlib.sha256("\n".join(chunks).encode()).hexdigest()
    db = get_service_db()
    now = datetime.now(timezone.utc).isoformat()

    existing_res = await asyncio.to_thread(
        lambda: db.table("page_summaries")
        .select("id, content_hash")
        .eq("source_id", source_id)
        .eq("url", url)
        .execute()
    )
    existing = (existing_res.data or [None])[0]

    if existing and existing.get("content_hash") == content_hash:
        update_payload: dict = {"updated_at": now}
        if session_id:
            update_payload["session_id"] = session_id
        await asyncio.to_thread(
            lambda: db.table("page_summaries")
            .update(update_payload)
            .eq("id", existing["id"])
            .execute()
        )
        return False

    result = await summarize_page(url, title, chunks)
    try:
        embedding = await get_embedding(result["summary"])
    except Exception as exc:
        logger.error("Embedding failed for page summary %s: %s", url, exc)
        embedding = None

    record = {
        "workspace_id": workspace_id,
        "source_id": source_id,
        "url": url,
        "title": title,
        "page_type": result["page_type"],
        "summary": result["summary"],
        "embedding": embedding,
        "content_hash": content_hash,
        "updated_at": now,
    }
    if session_id:
        record["session_id"] = session_id

    await asyncio.to_thread(
        lambda: db.table("page_summaries")
        .upsert(record, on_conflict="source_id,url")
        .execute()
    )
    return True
