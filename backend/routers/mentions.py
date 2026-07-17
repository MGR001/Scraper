import asyncio
import math
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from ..auth import WorkspaceContext, get_workspace
from ..database import get_service_db

router = APIRouter()

_MIN_AGGREGATE_N = 5    # below this, suppress the aggregate rather than show a shaky number
_MIN_SPIKE_MENTIONS = 5  # a spike needs real volume, not "1 mention today vs 0 usually"


@router.get("/")
async def list_mentions(
    source_id: str | None = None,
    signal_type: str | None = None,
    aspect: str | None = None,
    min_sentiment: float | None = None,
    max_sentiment: float | None = None,
    since: str | None = None,
    only_relevant: bool = True,
    limit: int = 40,
    offset: int = 0,
    ws: WorkspaceContext = Depends(get_workspace),
):
    """Mentions feed, newest first. relevant=true only by default — pass
    only_relevant=false to also see mentions the classifier judged
    irrelevant (useful for spot-checking the classifier itself)."""
    db = get_service_db()
    query = (
        db.table("mentions").select("*")
        .eq("workspace_id", ws.workspace_id)
        .order("published_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if only_relevant:
        query = query.eq("relevant", True)
    if source_id:
        query = query.eq("source_id", source_id)
    if signal_type:
        query = query.eq("signal_type", signal_type)
    if aspect:
        query = query.eq("aspect", aspect)
    if min_sentiment is not None:
        query = query.gte("sentiment", min_sentiment)
    if max_sentiment is not None:
        query = query.lte("sentiment", max_sentiment)
    if since:
        query = query.gte("published_at", since)

    result = await asyncio.to_thread(lambda: query.execute())
    return result.data or []


def _weighted_sentiment(rows: list[dict]) -> float | None:
    """Weights each mention's sentiment by ln(1 + max(score, 0)). Falls back
    to a plain average when every row has zero weight (e.g. all freshly
    fetched, zero-upvote posts) so a formula quirk doesn't hide real data."""
    weighted_sum = 0.0
    weight_total = 0.0
    sentiments: list[float] = []
    for m in rows:
        sentiment = m.get("sentiment")
        if sentiment is None:
            continue
        sentiments.append(sentiment)
        weight = math.log(1 + max(m.get("score") or 0, 0))
        weighted_sum += sentiment * weight
        weight_total += weight

    if weight_total > 0:
        return weighted_sum / weight_total
    return (sum(sentiments) / len(sentiments)) if sentiments else None


def _top_negative_aspect(rows: list[dict]) -> str | None:
    """Most frequent aspect among mentions with negative sentiment — the #1
    thing people are complaining about, not the aspect with the single
    lowest score."""
    negative_aspects = [
        m["aspect"] for m in rows
        if m.get("sentiment") is not None and m["sentiment"] < 0 and m.get("aspect")
    ]
    if not negative_aspects:
        return None
    return Counter(negative_aspects).most_common(1)[0][0]


def _mention_timestamp(m: dict, now: datetime) -> datetime | None:
    raw = m.get("published_at") or m.get("fetched_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts <= now else now  # clock-skew guard, never let a row count as "future"


def _is_spike(rows: list[dict], now: datetime | None = None) -> bool:
    """True when the last 24h relevant-mention count exceeds 5x the trailing
    7-day daily average (the 7 days before that last 24h window), with a
    minimum absolute volume so a single stray mention can't register as a
    5x spike over a near-zero baseline. A cheap volume signal, not full
    anomaly detection — surfaced as a badge, not an alert."""
    now = now or datetime.now(timezone.utc)
    last_24h_start = now - timedelta(hours=24)
    trailing_start = now - timedelta(days=8)

    last_24h = 0
    trailing_total = 0
    for m in rows:
        ts = _mention_timestamp(m, now)
        if ts is None:
            continue
        if ts >= last_24h_start:
            last_24h += 1
        elif ts >= trailing_start:
            trailing_total += 1

    if last_24h < _MIN_SPIKE_MENTIONS:
        return False
    trailing_avg = trailing_total / 7
    return last_24h > 5 * trailing_avg


def summarize_source_mentions(source: dict, rows: list[dict]) -> dict:
    """Builds one source's /summary entry. Fewer than 5 relevant mentions
    means {n, insufficient: true} instead of numbers — never render a stat
    built on a handful of data points as if it were solid."""
    n = len(rows)
    if n < _MIN_AGGREGATE_N:
        return {
            "source_id": source["id"], "source_name": source["name"],
            "n": n, "insufficient": True,
        }

    return {
        "source_id": source["id"], "source_name": source["name"],
        "n": n, "insufficient": False,
        "weighted_sentiment": _weighted_sentiment(rows),
        "switching_intent_count": sum(1 for m in rows if m.get("signal_type") == "switching_intent"),
        "top_negative_aspect": _top_negative_aspect(rows),
        "spike": _is_spike(rows),
    }


@router.get("/summary")
async def mentions_summary(ws: WorkspaceContext = Depends(get_workspace)):
    """Per-competitor-source rollup: mention count, weighted sentiment,
    switching-intent count, top negative aspect, spike flag."""
    db = get_service_db()

    sources_res = await asyncio.to_thread(
        lambda: db.table("sources").select("id, name")
        .eq("workspace_id", ws.workspace_id)
        .eq("category", "competitor")
        .execute()
    )
    sources = sources_res.data or []

    mentions_res = await asyncio.to_thread(
        lambda: db.table("mentions")
        .select("source_id, sentiment, score, signal_type, aspect, published_at, fetched_at")
        .eq("workspace_id", ws.workspace_id)
        .eq("relevant", True)
        .execute()
    )
    by_source: dict[str, list[dict]] = {}
    for m in (mentions_res.data or []):
        by_source.setdefault(m["source_id"], []).append(m)

    results = [
        summarize_source_mentions(source, by_source.get(source["id"], []))
        for source in sources
    ]
    return {"results": results}
