"""
Pure fetch layer for Reddit's public JSON endpoints. No classification,
no DB access — see mention_classifier.py and mention_monitor.py for those.

Built so OAuth can be layered in later (swap the client/header construction
inside get_http_client/_request) without touching any caller.
"""
import asyncio
import logging
import time

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.reddit.com"
_MIN_REQUEST_INTERVAL = 1.0  # seconds — unauthenticated Reddit politeness limit


class RedditError(Exception):
    """Raised for any Reddit fetch failure (network, blocked, malformed response)."""


# ── Shared HTTP client + throttle ───────────────────────────────────────────

_client: httpx.AsyncClient | None = None
_throttle_lock = asyncio.Lock()
_last_request_time = 0.0


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": f"Rivalry/1.0 (competitive intelligence; {settings.contact_email})"},
            follow_redirects=True,
            timeout=30,
        )
    return _client


async def close_http_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


async def _throttled_get(url: str, params: dict | None) -> httpx.Response:
    """Serializes all Reddit requests through a single ≥1s-apart gate."""
    global _last_request_time
    client = get_http_client()
    async with _throttle_lock:
        wait = _last_request_time + _MIN_REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise RedditError(f"Reddit request to {url} failed: {exc}") from exc
        finally:
            _last_request_time = time.monotonic()
    return response


async def _request(path: str, params: dict | None = None):
    """GETs a Reddit JSON endpoint. Retries once after a 30s backoff on 429;
    raises RedditError with a clear message on 403/blocked or bad responses."""
    url = f"{_BASE_URL}{path}"

    response = await _throttled_get(url, params)
    if response.status_code == 429:
        logger.warning("Reddit rate-limited request to %s, backing off 30s", path)
        await asyncio.sleep(30)
        response = await _throttled_get(url, params)

    if response.status_code == 403 or response.status_code == 429:
        raise RedditError(
            f"Reddit blocked request to {path} (status {response.status_code}). "
            "Check the User-Agent header and request rate."
        )
    if response.status_code != 200:
        raise RedditError(f"Reddit request to {path} failed with status {response.status_code}.")

    try:
        return response.json()
    except Exception as exc:
        raise RedditError(f"Reddit returned a non-JSON response for {path}: {exc}") from exc


# ── Post parsing ─────────────────────────────────────────────────────────────

def _parse_post(data: dict) -> dict:
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "title": data.get("title", ""),
        "selftext": data.get("selftext", ""),
        "author": data.get("author"),
        "score": data.get("score", 0),
        "num_comments": data.get("num_comments", 0),
        "created_utc": data.get("created_utc"),
        "permalink": data.get("permalink"),
        "subreddit": data.get("subreddit"),
    }


def _parse_post_listing(listing: dict) -> list[dict]:
    children = (listing or {}).get("data", {}).get("children", [])
    return [_parse_post(c["data"]) for c in children if c.get("kind") == "t3"]


# ── Public fetch functions ──────────────────────────────────────────────────

async def fetch_subreddit_new(subreddit: str, limit: int = 100) -> list[dict]:
    data = await _request(f"/r/{subreddit}/new.json", params={"limit": limit})
    return _parse_post_listing(data)


async def search_mentions(term: str, subreddit: str | None = None, limit: int = 50) -> list[dict]:
    q = f'"{term}"' if " " in term else term
    if subreddit:
        path = f"/r/{subreddit}/search.json"
        params = {"q": q, "restrict_sr": 1, "sort": "new", "limit": limit}
    else:
        path = "/search.json"
        params = {"q": q, "sort": "new", "limit": limit}
    data = await _request(path, params=params)
    return _parse_post_listing(data)


def _walk_comments(children: list[dict], thread_title: str, max_comments: int) -> list[dict]:
    """Recursively flattens a Reddit comment listing's `data.children` tree.
    Skips `more` stubs (not fetched in v1) and deleted/removed bodies, but
    still descends into their replies since a deleted parent can have live
    children. Stops once max_comments have been collected."""
    out: list[dict] = []

    def _visit(nodes: list[dict]) -> None:
        for node in nodes:
            if len(out) >= max_comments:
                return
            if node.get("kind") != "t1":
                continue  # skips "more" stubs and anything unrecognized
            d = node.get("data", {})
            body = d.get("body", "")
            if body not in ("[deleted]", "[removed]"):
                out.append({
                    "id": d.get("id"),
                    "name": d.get("name"),
                    "author": d.get("author"),
                    "body": body,
                    "score": d.get("score", 0),
                    "created_utc": d.get("created_utc"),
                    "permalink": d.get("permalink"),
                    "title": thread_title,
                })
            if len(out) >= max_comments:
                return
            replies = d.get("replies")
            if isinstance(replies, dict):
                _visit(replies.get("data", {}).get("children", []))

    _visit(children)
    return out[:max_comments]


async def fetch_comments(post_id: str, max_comments: int = 60) -> dict:
    data = await _request(f"/comments/{post_id}.json")
    if not isinstance(data, list) or len(data) < 2:
        raise RedditError(f"Unexpected comments response shape for post {post_id}.")

    post_children = data[0].get("data", {}).get("children", [])
    post = _parse_post(post_children[0]["data"]) if post_children else {}

    comment_children = data[1].get("data", {}).get("children", [])
    comments = _walk_comments(comment_children, post.get("title", ""), max_comments)

    return {"post": post, "comments": comments}
