import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _check_and_scrape() -> None:
    """Scrape every source whose next-scrape time has arrived."""
    from .database import get_db
    from .services.scraper import scrape_source

    db = get_db()
    try:
        result = await asyncio.to_thread(
            lambda: db.table("sources").select("*").eq("is_active", True).execute()
        )
    except Exception as exc:
        logger.error("Scheduler: failed to fetch sources: %s", exc)
        return

    now = datetime.now(timezone.utc)
    for source in result.data:
        last = source.get("last_scraped_at")
        interval_hours = source.get("scrape_interval", 24)

        if last is None:
            due = True
        else:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            due = (now - last_dt) >= timedelta(hours=interval_hours)

        if due:
            try:
                result = await scrape_source(source["id"], source["url"])
                logger.info("Scheduler scraped '%s': %s", source["name"], result)
            except Exception as exc:
                logger.error("Scheduler scrape failed for %s: %s", source["url"], exc)


async def _loop() -> None:
    """Hourly background loop."""
    while True:
        await _check_and_scrape()
        await asyncio.sleep(3600)


def start_scheduler() -> None:
    global _task
    _task = asyncio.create_task(_loop())
    logger.info("Background scheduler started.")


def stop_scheduler() -> None:
    global _task
    if _task:
        _task.cancel()
        logger.info("Background scheduler stopped.")
