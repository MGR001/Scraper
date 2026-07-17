"""
Reddit client tests — Task 2.

The comment-tree walker is pure and tested directly against fixture JSON
shaped like Reddit's real listing responses (nested replies, `more` stubs,
deleted/removed bodies). _request's throttle/retry/error handling is tested
with a mocked httpx client so no network call happens.

A live smoke test against r/legaltego is NOT included here — run manually:
    python -c "import asyncio; from backend.services.reddit import fetch_subreddit_new; \
        print(asyncio.run(fetch_subreddit_new('legaltech', limit=5)))"
"""
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.reddit import (
    RedditError,
    _request,
    _walk_comments,
    fetch_comments,
    search_mentions,
)


def _comment(id_, body, replies=None):
    return {
        "kind": "t1",
        "data": {
            "id": id_,
            "name": f"t1_{id_}",
            "author": "someone",
            "body": body,
            "score": 1,
            "created_utc": 1700000000,
            "permalink": f"/r/test/comments/abc/_/{id_}/",
            "replies": replies or "",
        },
    }


# ── Comment tree walker ──────────────────────────────────────────────────────

def test_walk_comments_nested_replies():
    tree = [
        _comment("a", "top level", replies={
            "data": {"children": [
                _comment("b", "reply to a", replies={
                    "data": {"children": [_comment("c", "reply to b")]}
                })
            ]}
        })
    ]
    result = _walk_comments(tree, "Thread Title", max_comments=60)
    assert [c["id"] for c in result] == ["a", "b", "c"]
    assert all(c["title"] == "Thread Title" for c in result)


def test_walk_comments_skips_more_stubs():
    tree = [_comment("a", "real comment"), {"kind": "more", "data": {"children": ["t1_x", "t1_y"]}}]
    result = _walk_comments(tree, "T", max_comments=60)
    assert [c["id"] for c in result] == ["a"]


def test_walk_comments_skips_deleted_and_removed_but_descends_into_replies():
    tree = [
        _comment("a", "[deleted]", replies={
            "data": {"children": [_comment("b", "live reply under a deleted parent")]}
        }),
        _comment("c", "[removed]"),
    ]
    result = _walk_comments(tree, "T", max_comments=60)
    assert [c["id"] for c in result] == ["b"]


def test_walk_comments_stops_at_max_comments():
    tree = [_comment(str(i), f"comment {i}") for i in range(10)]
    result = _walk_comments(tree, "T", max_comments=3)
    assert len(result) == 3


# ── search_mentions query construction ──────────────────────────────────────

@pytest.mark.asyncio
async def test_search_mentions_quotes_multiword_term_and_restricts_subreddit():
    with patch("backend.services.reddit._request", new_callable=AsyncMock) as mock_request:
        mock_request.return_value = {"data": {"children": []}}
        await search_mentions("Clio Grow", subreddit="legaltech")
        path, kwargs = mock_request.call_args[0][0], mock_request.call_args[1]
        assert path == "/r/legaltech/search.json"
        assert kwargs["params"]["q"] == '"Clio Grow"'
        assert kwargs["params"]["restrict_sr"] == 1


@pytest.mark.asyncio
async def test_search_mentions_single_word_unquoted_no_subreddit():
    with patch("backend.services.reddit._request", new_callable=AsyncMock) as mock_request:
        mock_request.return_value = {"data": {"children": []}}
        await search_mentions("Leya")
        path, kwargs = mock_request.call_args[0][0], mock_request.call_args[1]
        assert path == "/search.json"
        assert kwargs["params"]["q"] == "Leya"


# ── fetch_comments assembly ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_comments_assembles_post_and_flattened_comments():
    raw = [
        {"data": {"children": [{"kind": "t3", "data": {
            "id": "abc123", "name": "t3_abc123", "title": "Anyone tried Legora?",
            "selftext": "curious", "author": "u1", "score": 10, "num_comments": 2,
            "created_utc": 1700000000, "permalink": "/r/legaltech/comments/abc123/x/",
            "subreddit": "legaltech",
        }}]}},
        {"data": {"children": [_comment("c1", "I switched last month")]}},
    ]
    with patch("backend.services.reddit._request", new_callable=AsyncMock, return_value=raw):
        result = await fetch_comments("abc123", max_comments=60)

    assert result["post"]["title"] == "Anyone tried Legora?"
    assert result["comments"][0]["title"] == "Anyone tried Legora?"
    assert result["comments"][0]["body"] == "I switched last month"


# ── _request throttle/retry/error handling ──────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


@pytest.mark.asyncio
async def test_request_retries_once_on_429_then_succeeds():
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=[_FakeResponse(429), _FakeResponse(200, {"ok": True})])

    with patch("backend.services.reddit.get_http_client", return_value=fake_client), \
         patch("backend.services.reddit.asyncio.sleep", new_callable=AsyncMock):
        result = await _request("/r/test/new.json")

    assert result == {"ok": True}
    assert fake_client.get.call_count == 2


@pytest.mark.asyncio
async def test_request_raises_after_second_consecutive_429():
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=[_FakeResponse(429), _FakeResponse(429)])

    with patch("backend.services.reddit.get_http_client", return_value=fake_client), \
         patch("backend.services.reddit.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RedditError):
            await _request("/r/test/new.json")


@pytest.mark.asyncio
async def test_request_raises_clear_message_on_403():
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=_FakeResponse(403))

    with patch("backend.services.reddit.get_http_client", return_value=fake_client), \
         patch("backend.services.reddit.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RedditError, match="blocked"):
            await _request("/r/test/new.json")
