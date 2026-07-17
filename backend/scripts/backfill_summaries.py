"""
Backfill page_summaries from existing scraped_content.

Usage:
    python -m backend.scripts.backfill_summaries [--workspace <id>] [--dry-run]

Idempotent — store_page_summary's content-hash check skips any page whose
summary is already current, so a re-run only pays for pages that are new or
have changed since the last backfill/sweep.
"""
import argparse
import asyncio
import logging

from ..database import get_service_db
from ..services.summarizer import store_page_summary

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def _fetch_active_sources(db, workspace_id: str | None) -> list[dict]:
    query = db.table("sources").select("id, name, workspace_id").eq("is_active", True)
    if workspace_id:
        query = query.eq("workspace_id", workspace_id)
    res = await asyncio.to_thread(lambda: query.execute())
    return res.data or []


async def _fetch_content_grouped_by_url(db, source_id: str) -> dict[str, list[dict]]:
    res = await asyncio.to_thread(
        lambda: db.table("scraped_content")
        .select("url, title, content, metadata")
        .eq("source_id", source_id)
        .execute()
    )
    by_url: dict[str, list[dict]] = {}
    for row in (res.data or []):
        by_url.setdefault(row["url"], []).append(row)
    for rows in by_url.values():
        rows.sort(key=lambda r: (r.get("metadata") or {}).get("chunk_index", 0))
    return by_url


async def _backfill_source(db, src: dict, dry_run: bool) -> tuple[int, int, int]:
    """Returns (summarized, skipped, failed) for this source."""
    by_url = await _fetch_content_grouped_by_url(db, src["id"])
    total = len(by_url)
    summarized = skipped = failed = 0

    if dry_run:
        logger.info("[dry-run] %s: would process %d page(s)", src["name"], total)
        return total, 0, 0

    for i, (url, rows) in enumerate(by_url.items(), 1):
        title = rows[0].get("title") or ""
        chunks = [r["content"] for r in rows if r.get("content")]
        if not chunks:
            continue
        try:
            generated = await store_page_summary(src["workspace_id"], src["id"], url, title, chunks)
            if generated:
                summarized += 1
            else:
                skipped += 1
        except Exception as exc:
            failed += 1
            logger.warning("  failed to summarise %s: %s", url, exc)
        await asyncio.sleep(0.2)  # polite rate limit

        if i % 20 == 0 or i == total:
            logger.info("%s: %d/%d pages, %d summarised, %d skipped, %d failed",
                        src["name"], i, total, summarized, skipped, failed)

    return summarized, skipped, failed


async def backfill(workspace_id: str | None, dry_run: bool) -> None:
    db = get_service_db()
    sources = await _fetch_active_sources(db, workspace_id)
    logger.info("Backfilling %d active source(s)%s%s", len(sources),
                f" for workspace {workspace_id}" if workspace_id else "",
                " [DRY RUN — no model calls]" if dry_run else "")

    grand_summarized = grand_skipped = grand_failed = 0
    for src in sources:
        summarized, skipped, failed = await _backfill_source(db, src, dry_run)
        grand_summarized += summarized
        grand_skipped += skipped
        grand_failed += failed

    if dry_run:
        logger.info("Dry run complete. %d page(s) would be considered across %d source(s).",
                    grand_summarized, len(sources))
    else:
        logger.info(
            "Backfill complete. %d summarised (LLM calls made), %d skipped (unchanged), %d failed.",
            grand_summarized, grand_skipped, grand_failed,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill page_summaries from existing scraped_content.")
    parser.add_argument("--workspace", default=None, help="Limit to a single workspace id.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be summarised without calling the model.")
    args = parser.parse_args()
    asyncio.run(backfill(args.workspace, args.dry_run))


if __name__ == "__main__":
    main()
