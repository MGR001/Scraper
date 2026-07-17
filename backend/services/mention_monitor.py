"""
Reddit mention ingestion sweep (tasks.md Task 4).

One entry point, check_mentions_for_workspace, run per-workspace by the
scheduler after each scrape sweep. Never raises — a single competitor
source's failure is logged and skipped so the rest of the workspace's
sweep continues.
"""
import asyncio
import logging
from datetime import datetime, timezone

from ..config import settings
from .mention_classifier import classify_and_store
from .reddit import RedditError, fetch_comments, fetch_subreddit_new, search_mentions

logger = logging.getLogger(__name__)


def _terms(source: dict) -> list[str]:
    return [t for t in (source.get("mention_terms") or [source.get("name", "")]) if t]


def _matches(text: str, terms: list[str]) -> bool:
    text_l = text.lower()
    return any(t.lower() in text_l for t in terms)


async def _get_high_water(db, workspace_id: str, stream_key: str) -> int:
    res = await asyncio.to_thread(
        lambda: db.table("mention_streams").select("last_seen_utc")
        .eq("workspace_id", workspace_id).eq("platform", "reddit").eq("stream_key", stream_key)
        .execute()
    )
    rows = res.data or []
    return rows[0]["last_seen_utc"] if rows else 0


async def _set_high_water(db, workspace_id: str, stream_key: str, value: int) -> None:
    await asyncio.to_thread(
        lambda: db.table("mention_streams").upsert({
            "workspace_id": workspace_id, "platform": "reddit",
            "stream_key": stream_key, "last_seen_utc": value,
        }, on_conflict="workspace_id,platform,stream_key").execute()
    )


async def _set_checked_at(db, source_id: str) -> None:
    await asyncio.to_thread(
        lambda: db.table("sources")
        .update({"mentions_checked_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", source_id).execute()
    )


class _Budget:
    """Tracks the sweep's LLM-call budget. Same pattern as
    settings.max_summaries_per_sweep — once exhausted, remaining items are
    skipped for this sweep rather than raising."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.exhausted = limit <= 0

    def spend(self, made_call: bool) -> None:
        if made_call:
            self.used += 1
        if self.used >= self.limit:
            self.exhausted = True


async def _classify_matching(
    db, workspace_id: str, sources: list[dict], item: dict, kind: str,
    counts: dict, budget: _Budget, parent_id: str | None = None,
) -> None:
    """Classifies item against every source whose terms appear in its text —
    an item mentioning two watched rivals produces one row per rival."""
    body_field = "selftext" if kind == "post" else "body"
    text = f"{item.get('title', '')} {item.get(body_field, '')}"
    for source in sources:
        if budget.exhausted:
            return
        if not _matches(text, _terms(source)):
            continue
        record = dict(item)
        if parent_id:
            record["parent_id"] = parent_id
        made_call = await classify_and_store(db, workspace_id, source, record, kind)
        budget.spend(made_call)
        counts["classified" if made_call else "skipped_dedupe"] += 1


async def _expand_post(
    db, workspace_id: str, sources: list[dict], post: dict, counts: dict, budget: _Budget,
) -> None:
    """Classifies a post against every matching source, then expands its
    comment tree and does the same for each comment."""
    await _classify_matching(db, workspace_id, sources, post, "post", counts, budget)
    if budget.exhausted:
        return

    try:
        detail = await fetch_comments(post["id"], max_comments=60)
    except RedditError as exc:
        logger.warning("mentions: fetch_comments failed for post %s: %s", post.get("id"), exc)
        return

    counts["fetched"] += len(detail["comments"])
    for comment in detail["comments"]:
        if budget.exhausted:
            return
        await _classify_matching(
            db, workspace_id, sources, comment, "comment", counts, budget,
            parent_id=post.get("name"),
        )


async def check_mentions_for_workspace(db, workspace_id: str) -> dict:
    """Polls Reddit for every competitor source with mentions_enabled in this
    workspace, classifies matches, and stores them. Returns sweep counts:
    {fetched, classified, relevant, skipped_dedupe}."""
    sources_res = await asyncio.to_thread(
        lambda: db.table("sources").select("*")
        .eq("workspace_id", workspace_id)
        .eq("category", "competitor")
        .eq("mentions_enabled", True)
        .eq("is_active", True)
        .execute()
    )
    sources = sources_res.data or []
    counts = {"fetched": 0, "classified": 0, "relevant": 0, "skipped_dedupe": 0}
    if not sources:
        return counts

    sweep_started_at = datetime.now(timezone.utc).isoformat()
    budget = _Budget(settings.max_mention_classifications_per_sweep)
    processed_post_ids: set[str] = set()

    # ── 1. Targeted search: term x subreddit, plus one global search per term ──
    for source in sources:
        if budget.exhausted:
            break
        try:
            checked_at = source.get("mentions_checked_at")
            checked_epoch = (
                datetime.fromisoformat(checked_at.replace("Z", "+00:00")).timestamp()
                if checked_at else 0
            )
            terms = _terms(source)
            subs = source.get("mention_subreddits") or []

            for term in terms:
                if budget.exhausted:
                    break
                for sub in [None, *subs]:
                    try:
                        posts = await search_mentions(term, subreddit=sub, limit=50)
                    except RedditError as exc:
                        logger.warning("mentions: search failed for %r (source %s): %s",
                                       term, source["id"], exc)
                        continue
                    counts["fetched"] += len(posts)
                    for p in posts:
                        if budget.exhausted:
                            break
                        if p["name"] in processed_post_ids:
                            continue
                        if (p.get("created_utc") or 0) <= checked_epoch:
                            continue
                        processed_post_ids.add(p["name"])
                        # Classify against every watched competitor, not just
                        # the one whose search turned this post up.
                        await _expand_post(db, workspace_id, sources, p, counts, budget)
                    if budget.exhausted:
                        break

            await _set_checked_at(db, source["id"])
        except Exception as exc:
            logger.error("mentions: targeted search failed for source %s: %s", source["id"], exc)

    # ── 2. Stream watch: each distinct subreddit polled once, workspace-wide ──
    if not budget.exhausted:
        all_subs = sorted({sub for s in sources for sub in (s.get("mention_subreddits") or [])})
        for sub in all_subs:
            if budget.exhausted:
                break
            stream_key = f"r/{sub}/new"
            try:
                posts = await fetch_subreddit_new(sub, limit=100)
            except RedditError as exc:
                logger.error("mentions: fetch_subreddit_new failed for r/%s: %s", sub, exc)
                continue

            counts["fetched"] += len(posts)
            high_water = await _get_high_water(db, workspace_id, stream_key)
            new_high_water = high_water

            try:
                for p in posts:
                    created = p.get("created_utc") or 0
                    if created <= high_water:
                        continue
                    new_high_water = max(new_high_water, created)
                    if p["name"] in processed_post_ids:
                        continue
                    text = f"{p.get('title', '')} {p.get('selftext', '')}"
                    any_term_match = any(_matches(text, _terms(s)) for s in sources)
                    if not (any_term_match or p.get("num_comments", 0) > 3):
                        continue
                    if budget.exhausted:
                        break
                    processed_post_ids.add(p["name"])
                    await _expand_post(db, workspace_id, sources, p, counts, budget)
                await _set_high_water(db, workspace_id, stream_key, new_high_water)
            except Exception as exc:
                logger.error("mentions: stream watch failed for r/%s: %s", sub, exc)

    if budget.exhausted:
        logger.warning(
            "mentions: hit max_mention_classifications_per_sweep (%d) for workspace %s — "
            "remaining items skipped this sweep",
            settings.max_mention_classifications_per_sweep, workspace_id,
        )

    relevant_res = await asyncio.to_thread(
        lambda: db.table("mentions").select("id", count="exact")
        .eq("workspace_id", workspace_id)
        .eq("relevant", True)
        .gte("fetched_at", sweep_started_at)
        .execute()
    )
    counts["relevant"] = relevant_res.count or 0

    logger.info(
        "mentions: %d fetched, %d classified, %d relevant, %d skipped(dedupe)",
        counts["fetched"], counts["classified"], counts["relevant"], counts["skipped_dedupe"],
    )
    return counts
