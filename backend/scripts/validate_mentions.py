"""
Manual validation harness for the Reddit mention classifier (tasks.md Task 3).

Pulls real posts + comments for a competitor from Reddit, classifies each one,
and writes the results to CSV for a human to hand-check relevance precision
and gross sentiment direction. Does NOT touch the mentions table or any
source's config — this is throwaway output for review, not ingestion. Per
tasks.md, Task 4 (automated sweeps) should not be wired up until this
validation pass looks good (relevance precision roughly >= 85%).

Usage:
    python -m backend.scripts.validate_mentions \
        --competitor "Legora" --terms "legora,legora ai" \
        --subreddits legaltech,LawFirm --limit 25 --out mentions_review.csv
"""
import argparse
import asyncio
import csv
import logging

from ..services.mention_classifier import classify_mention
from ..services.reddit import RedditError, close_http_client, fetch_comments, fetch_subreddit_new, search_mentions

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_FIELDS = [
    "kind", "subreddit", "url", "title", "body",
    "relevant", "confidence", "sentiment", "aspect", "signal_type",
    "is_firsthand", "summary",
]


async def _gather_posts(terms: list[str], subreddits: list[str], limit: int) -> list[dict]:
    """Same shape as the planned Task 4 targeted search: each term gets one
    global search plus one restricted search per configured subreddit."""
    posts_by_id: dict[str, dict] = {}

    for term in terms:
        try:
            for p in await search_mentions(term, subreddit=None, limit=limit):
                posts_by_id[p["name"]] = p
        except RedditError as exc:
            logger.warning("search_mentions(%r) failed: %s", term, exc)
        for sub in subreddits:
            try:
                for p in await search_mentions(term, subreddit=sub, limit=limit):
                    posts_by_id[p["name"]] = p
            except RedditError as exc:
                logger.warning("search_mentions(%r, r/%s) failed: %s", term, sub, exc)

    for sub in subreddits:
        try:
            found = await fetch_subreddit_new(sub, limit=limit)
        except RedditError as exc:
            logger.warning("fetch_subreddit_new(r/%s) failed: %s", sub, exc)
            continue
        for p in found:
            text = f"{p.get('title', '')} {p.get('selftext', '')}".lower()
            if any(t.lower() in text for t in terms):
                posts_by_id[p["name"]] = p

    return list(posts_by_id.values())


async def validate(
    competitor: str, terms: list[str], subreddits: list[str],
    limit: int, max_items: int, out_path: str,
) -> None:
    posts = await _gather_posts(terms, subreddits, limit)
    logger.info("Found %d candidate post(s)", len(posts))

    rows: list[dict] = []

    for post in posts:
        if len(rows) >= max_items:
            break

        classification = await classify_mention(
            competitor, terms, post.get("title", ""), post.get("selftext", ""), is_comment=False,
        )
        rows.append({
            "kind": "post",
            "subreddit": post.get("subreddit"),
            "url": f"https://www.reddit.com{post.get('permalink', '')}",
            "title": post.get("title", ""),
            "body": (post.get("selftext") or "")[:500],
            **classification,
        })
        logger.info("[post] %s -> relevant=%s sentiment=%s", post.get("title", "")[:60],
                    classification["relevant"], classification["sentiment"])

        try:
            detail = await fetch_comments(post["id"], max_comments=20)
        except RedditError as exc:
            logger.warning("fetch_comments(%s) failed: %s", post["id"], exc)
            continue

        for comment in detail["comments"]:
            if len(rows) >= max_items:
                break
            text = f"{comment.get('title', '')} {comment.get('body', '')}".lower()
            if not any(t.lower() in text for t in terms):
                continue  # only classify comments that actually mention a watched term
            classification = await classify_mention(
                competitor, terms, comment.get("title", ""), comment.get("body", ""), is_comment=True,
            )
            rows.append({
                "kind": "comment",
                "subreddit": post.get("subreddit"),
                "url": f"https://www.reddit.com{comment['permalink']}" if comment.get("permalink") else "",
                "title": comment.get("title", ""),
                "body": (comment.get("body") or "")[:500],
                **classification,
            })

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    relevant = sum(1 for r in rows if r["relevant"] is True)
    logger.info("Wrote %d classified item(s) to %s (%d marked relevant)", len(rows), out_path, relevant)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--competitor", required=True, help="Competitor display name, e.g. 'Legora'")
    parser.add_argument("--terms", required=True, help="Comma-separated aliases, e.g. 'legora,legora ai'")
    parser.add_argument("--subreddits", default="", help="Comma-separated subreddits, e.g. 'legaltech,LawFirm'")
    parser.add_argument("--limit", type=int, default=25, help="Max results per search/listing call")
    parser.add_argument("--max-items", type=int, default=100, help="Stop once this many rows are classified")
    parser.add_argument("--out", default="mentions_review.csv", help="Output CSV path")
    args = parser.parse_args()

    terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    subreddits = [s.strip() for s in args.subreddits.split(",") if s.strip()]
    if not terms:
        parser.error("--terms must include at least one term")

    async def _run():
        try:
            await validate(args.competitor, terms, subreddits, args.limit, args.max_items, args.out)
        finally:
            await close_http_client()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
