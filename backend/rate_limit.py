"""
Simple in-memory rate limiter for expensive endpoints.
Limits are per workspace_id. Reset every WINDOW_SECONDS.
"""
import time
from collections import defaultdict
from fastapi import HTTPException

# { workspace_id: { endpoint_key: (count, window_start) } }
_counters: dict[str, dict[str, tuple[int, float]]] = defaultdict(dict)

LIMITS: dict[str, int] = {
    "chat":        20,   # per hour
    "comparison":   5,
    "gtm_heatmap":  5,
    "positioning":  5,
    "messaging":    5,
    "news_digest": 10,
}
WINDOW_SECONDS = 3600  # 1 hour


def check_rate_limit(workspace_id: str, key: str) -> None:
    """Raise 429 if the workspace has exceeded the hourly limit for *key*."""
    limit = LIMITS.get(key)
    if limit is None:
        return

    now = time.monotonic()
    count, window_start = _counters[workspace_id].get(key, (0, now))

    if now - window_start > WINDOW_SECONDS:
        count, window_start = 0, now

    if count >= limit:
        retry_after = int(WINDOW_SECONDS - (now - window_start))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded for '{key}'. "
                   f"Limit: {limit} per hour. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    _counters[workspace_id][key] = (count + 1, window_start)
