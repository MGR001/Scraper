import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _check_and_scrape() -> None:
    """Scrape every source whose next-scrape time has arrived, scoped per workspace."""
    # The scheduler uses the service-role client — it has no user JWT.
    # This is the ONE place the service key legitimately bypasses RLS.
    from .database import get_service_db
    from .services.mention_monitor import check_mentions_for_workspace
    from .services.scraper import scrape_source

    db = get_service_db()
    try:
        # Fetch all active workspaces where scraping is enabled
        ws_result = await asyncio.to_thread(
            lambda: db.table("workspaces")
            .select("id, crawl_max_pages, scrape_enabled, scrape_frequency")
            .eq("scrape_enabled", True)
            .execute()
        )
    except Exception as exc:
        logger.error("Scheduler: failed to fetch workspaces: %s", exc)
        return

    now = datetime.now(timezone.utc)

    for workspace in (ws_result.data or []):
        ws_id       = workspace["id"]
        max_pages   = workspace.get("crawl_max_pages") or 50
        frequency   = workspace.get("scrape_frequency") or "daily"
        interval_h  = {"hourly": 1, "daily": 24, "weekly": 168}.get(frequency, 24)

        try:
            src_result = await asyncio.to_thread(
                lambda w=ws_id: db.table("sources")
                .select("*")
                .eq("workspace_id", w)
                .eq("is_active", True)
                .execute()
            )
        except Exception as exc:
            logger.error("Scheduler: failed to fetch sources for workspace %s: %s", ws_id, exc)
            continue

        for source in (src_result.data or []):
            last = source.get("last_scraped_at")
            src_interval = source.get("scrape_interval") or interval_h

            due = last is None or (
                now - datetime.fromisoformat(last.replace("Z", "+00:00"))
                >= timedelta(hours=src_interval)
            )

            if due:
                try:
                    outcome = await scrape_source(
                        source["id"], source["url"],
                        max_pages=max_pages, workspace_id=ws_id,
                        crawl_scope=source.get("crawl_scope", "domain"),
                        sitemap_url=source.get("sitemap_url"),
                    )
                    logger.info("Scheduler scraped '%s' (ws=%s): %s",
                                source["name"], ws_id, outcome)
                except Exception as exc:
                    logger.error("Scheduler scrape failed for %s (ws=%s): %s",
                                 source["url"], ws_id, exc)

        try:
            mention_counts = await check_mentions_for_workspace(db, ws_id)
            if mention_counts.get("fetched") or mention_counts.get("classified"):
                logger.info("Scheduler mentions sweep (ws=%s): %s", ws_id, mention_counts)
        except Exception as exc:
            logger.error("Scheduler mentions sweep failed (ws=%s): %s", ws_id, exc)


async def _loop() -> None:
    """Hourly background loop."""
    while True:
        try:
            await _check_and_scrape()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Scheduler loop error (will retry next cycle): %s", exc)
        await asyncio.sleep(3600)


def start_scheduler() -> None:
    global _task
    _task = asyncio.create_task(_loop())
    logger.info("Background scheduler started.")


async def _stop_scheduler_async() -> None:
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
        logger.info("Background scheduler stopped.")


def stop_scheduler() -> None:
    global _task
    if _task:
        asyncio.ensure_future(_stop_scheduler_async())

