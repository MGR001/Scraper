"""
In-memory scrape status tracker.
Tracks the current state of each source's scrape job.
Resets on server restart (intentional — status is ephemeral).
"""
from datetime import datetime, timezone
from typing import Literal

ScrapeState = Literal["idle", "running", "completed", "error"]

_status: dict[str, dict] = {}


def set_status(source_id: str, state: ScrapeState, detail: str = "", new_chunks: int = 0) -> None:
    _status[source_id] = {
        "state": state,
        "detail": detail,
        "new_chunks": new_chunks,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_status(source_id: str) -> dict:
    return _status.get(
        source_id,
        {"state": "idle", "detail": "", "updated_at": None},
    )


def get_all_statuses() -> dict[str, dict]:
    return dict(_status)
