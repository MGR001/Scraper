import asyncio
import json
import logging
from datetime import datetime, timezone

from ..config import settings
from .llm import _create_completion

logger = logging.getLogger(__name__)

_VALID_ASPECTS = {
    "pricing", "support", "product", "onboarding", "reliability", "docs", "other",
}
_VALID_SIGNAL_TYPES = {
    "complaint", "praise", "question", "comparison", "switching_intent", "other",
}


def _system_prompt(competitor_name: str, terms: list[str]) -> str:
    terms_str = ", ".join(terms) if terms else competitor_name
    return (
        f'You classify a Reddit post or comment for competitive intelligence about\n'
        f'the company "{competitor_name}" (also known as: {terms_str}).\n\n'
        'Return ONLY valid JSON:\n'
        '{"relevant": bool, "confidence": 0..1, "sentiment": -1..1,\n'
        ' "aspect": "pricing|support|product|onboarding|reliability|docs|other",\n'
        ' "signal_type": "complaint|praise|question|comparison|switching_intent|other",\n'
        ' "is_firsthand": bool, "summary": "<max 25 words>"}\n\n'
        'relevant=false if: the text is not about this company as a product/service,\n'
        "is the company's own marketing or an employee, or the name match is\n"
        'coincidental. When relevant=false, other fields may be null.\n'
        'is_firsthand=true only if the author describes their own direct experience.\n'
        'signal_type=switching_intent when the author states they are leaving,\n'
        'have left, or are actively evaluating alternatives to this company.\n'
        'Reddit sarcasm is common — judge tone, not surface words.\n'
        'Use the thread title for context; a short comment like "same here"\n'
        'inherits the meaning of the thread.'
    )


def _blank_result() -> dict:
    return {
        "relevant": None, "confidence": None, "sentiment": None,
        "aspect": None, "signal_type": None, "is_firsthand": None, "summary": None,
    }


def _parse_classification(raw: str) -> dict:
    """Strips code fences, validates enums, and clamps numeric ranges. Never
    raises — any parse failure or missing/invalid `relevant` falls back to
    relevant=None with every other field null."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
    except Exception as exc:
        logger.warning("Failed to parse mention classification JSON: %s", exc)
        return _blank_result()

    if not isinstance(data, dict):
        logger.warning("Mention classification JSON was not an object: %r", data)
        return _blank_result()

    relevant = data.get("relevant")
    if not isinstance(relevant, bool):
        return _blank_result()

    result = _blank_result()
    result["relevant"] = relevant

    confidence = data.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        result["confidence"] = max(0.0, min(1.0, float(confidence)))

    sentiment = data.get("sentiment")
    if isinstance(sentiment, (int, float)) and not isinstance(sentiment, bool):
        result["sentiment"] = max(-1.0, min(1.0, float(sentiment)))

    aspect = data.get("aspect")
    if aspect in _VALID_ASPECTS:
        result["aspect"] = aspect

    signal_type = data.get("signal_type")
    if signal_type in _VALID_SIGNAL_TYPES:
        result["signal_type"] = signal_type

    is_firsthand = data.get("is_firsthand")
    if isinstance(is_firsthand, bool):
        result["is_firsthand"] = is_firsthand

    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip():
        result["summary"] = summary.strip()

    return result


async def classify_mention(
    competitor_name: str, terms: list[str], thread_title: str, body: str, is_comment: bool,
) -> dict:
    """Returns the parsed classification dict (see _blank_result for shape).
    Never raises — model/network failures are logged and return relevant=None
    so a bad LLM response never blocks ingestion."""
    kind_label = "Comment" if is_comment else "Post"
    user_content = (
        f"Thread title: {thread_title or '(no title)'}\n\n"
        f"{kind_label} text:\n{body or '(empty)'}"
    )

    try:
        response = await _create_completion(
            model=settings.classifier_model,
            messages=[
                {"role": "system", "content": _system_prompt(competitor_name, terms)},
                {"role": "user", "content": user_content},
            ],
            max_completion_tokens=300,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
    except Exception as exc:
        logger.error("Mention classification call failed for '%s': %s", competitor_name, exc)
        return _blank_result()

    return _parse_classification(raw)


def _reddit_permalink_url(item: dict) -> str:
    permalink = item.get("permalink") or ""
    if permalink.startswith("http"):
        return permalink
    return f"https://www.reddit.com{permalink}" if permalink else ""


def _published_at(item: dict) -> str | None:
    created_utc = item.get("created_utc")
    if not created_utc:
        return None
    return datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat()


async def classify_and_store(db, workspace_id: str, source: dict, item: dict, kind: str) -> bool:
    """Dedupes on (platform, external_id, source_id) before touching the LLM —
    an existing row means zero classification cost. Returns True iff an LLM
    call was made, so callers can track sweep-level classification counts."""
    external_id = item.get("name")
    source_id = source["id"]

    existing = await asyncio.to_thread(
        lambda: db.table("mentions").select("id")
        .eq("platform", "reddit")
        .eq("external_id", external_id)
        .eq("source_id", source_id)
        .execute()
    )
    if existing.data:
        return False

    is_comment = kind == "comment"
    title = item.get("title", "")
    body = item.get("body") if is_comment else item.get("selftext", "")
    terms = source.get("mention_terms") or [source.get("name", "")]
    competitor_name = source.get("name", "")

    classification = await classify_mention(competitor_name, terms, title, body, is_comment)

    record = {
        "workspace_id": workspace_id,
        "source_id": source_id,
        "platform": "reddit",
        "external_id": external_id,
        "parent_id": item.get("parent_id"),
        "kind": kind,
        "url": _reddit_permalink_url(item),
        "subreddit": item.get("subreddit"),
        "author": item.get("author"),
        "title": title,
        "body": body,
        "score": item.get("score", 0),
        "published_at": _published_at(item),
        **classification,
    }

    await asyncio.to_thread(
        lambda: db.table("mentions").insert(record).execute()
    )
    return True
