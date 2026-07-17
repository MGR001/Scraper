"""
Mention ingestion sweep tests — Task 4.

reddit.py's fetch functions and mention_classifier.classify_and_store are
mocked throughout — this file tests check_mentions_for_workspace's own
orchestration: which sources get queried, the budget guard, cross-rival
fan-out, and high-water-mark bookkeeping. Not a live-network test.
"""
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.mention_monitor import check_mentions_for_workspace


class _FakeTable:
    def __init__(self, db, name):
        self.db = db
        self.name = name
        self._filters: dict = {}
        self._mode = None
        self._payload = None
        self._select_count = None

    def select(self, *_args, **kwargs):
        self._mode = "select"
        self._select_count = kwargs.get("count")
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def gte(self, key, value):
        self._filters[f"gte:{key}"] = value
        return self

    def update(self, payload):
        self._mode = "update"
        self._payload = payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._mode = "upsert"
        self._payload = payload
        return self

    def execute(self):
        return self.db._execute(self.name, self._mode, self._filters, self._payload, self._select_count)


class _FakeDB:
    def __init__(self):
        self.sources: list[dict] = []
        self.mention_streams: list[dict] = []
        self.mentions: list[dict] = []
        self.updates: list[tuple] = []

    def table(self, name):
        return _FakeTable(self, name)

    def _execute(self, name, mode, filters, payload, select_count):
        rows = getattr(self, name)

        def _match(r):
            for k, v in filters.items():
                if k.startswith("gte:"):
                    if (r.get(k[4:]) or "") < v:
                        return False
                elif r.get(k) != v:
                    return False
            return True

        if mode == "select":
            matched = [r for r in rows if _match(r)]
            return type("Result", (), {"data": matched, "count": len(matched) if select_count else None})()

        if mode == "update":
            for r in rows:
                if _match(r):
                    r.update(payload)
            self.updates.append((name, dict(filters), dict(payload)))
            return type("Result", (), {"data": rows})()

        if mode == "upsert":
            key_fields = ("workspace_id", "platform", "stream_key")
            for r in rows:
                if all(r.get(k) == payload.get(k) for k in key_fields):
                    r.update(payload)
                    return type("Result", (), {"data": [r]})()
            rows.append(dict(payload))
            return type("Result", (), {"data": [payload]})()

        raise AssertionError("execute() called with no operation set")


def _source(**overrides):
    base = {
        "id": "src1", "workspace_id": "ws1", "name": "Legora", "category": "competitor",
        "mentions_enabled": True, "is_active": True,
        "mention_terms": ["legora"], "mention_subreddits": [], "mentions_checked_at": None,
    }
    base.update(overrides)
    return base


def _post(**overrides):
    base = {
        "id": "p1", "name": "t3_p1", "title": "Legora thread", "selftext": "",
        "author": "u1", "score": 5, "num_comments": 0, "created_utc": 1700000000,
        "permalink": "/r/x/comments/p1/", "subreddit": "legaltech",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_no_enabled_sources_returns_zero_counts_and_makes_no_calls():
    db = _FakeDB()
    with patch("backend.services.mention_monitor.search_mentions", new_callable=AsyncMock) as mock_search, \
         patch("backend.services.mention_monitor.fetch_subreddit_new", new_callable=AsyncMock) as mock_stream, \
         patch("backend.services.mention_monitor.classify_and_store", new_callable=AsyncMock) as mock_classify:
        counts = await check_mentions_for_workspace(db, "ws1")

    assert counts == {"fetched": 0, "classified": 0, "relevant": 0, "skipped_dedupe": 0}
    mock_search.assert_not_called()
    mock_stream.assert_not_called()
    mock_classify.assert_not_called()


@pytest.mark.asyncio
async def test_targeted_search_classifies_new_post_and_updates_checked_at():
    db = _FakeDB()
    db.sources = [_source()]
    post = _post()

    with patch("backend.services.mention_monitor.search_mentions", new_callable=AsyncMock, return_value=[post]), \
         patch("backend.services.mention_monitor.fetch_subreddit_new", new_callable=AsyncMock, return_value=[]), \
         patch("backend.services.mention_monitor.fetch_comments", new_callable=AsyncMock,
               return_value={"post": post, "comments": []}), \
         patch("backend.services.mention_monitor.classify_and_store", new_callable=AsyncMock, return_value=True) as mock_classify:
        counts = await check_mentions_for_workspace(db, "ws1")

    assert counts["classified"] == 1
    assert counts["skipped_dedupe"] == 0
    mock_classify.assert_called_once()
    assert any(u[0] == "sources" and u[1].get("id") == "src1" for u in db.updates)


@pytest.mark.asyncio
async def test_dedupe_skip_is_counted_not_classified():
    db = _FakeDB()
    db.sources = [_source()]
    post = _post()

    with patch("backend.services.mention_monitor.search_mentions", new_callable=AsyncMock, return_value=[post]), \
         patch("backend.services.mention_monitor.fetch_subreddit_new", new_callable=AsyncMock, return_value=[]), \
         patch("backend.services.mention_monitor.fetch_comments", new_callable=AsyncMock,
               return_value={"post": post, "comments": []}), \
         patch("backend.services.mention_monitor.classify_and_store", new_callable=AsyncMock, return_value=False):
        counts = await check_mentions_for_workspace(db, "ws1")

    assert counts["classified"] == 0
    assert counts["skipped_dedupe"] == 1


@pytest.mark.asyncio
async def test_post_mentioning_two_rivals_produces_two_classify_calls():
    db = _FakeDB()
    db.sources = [
        _source(id="src1", name="Legora", mention_terms=["legora"]),
        _source(id="src2", name="Leya", mention_terms=["leya"]),
    ]
    post = _post(title="Legora vs Leya, which is better?")

    async def fake_search(term, subreddit=None, limit=50):
        return [post] if term == "legora" else []

    with patch("backend.services.mention_monitor.search_mentions", side_effect=fake_search), \
         patch("backend.services.mention_monitor.fetch_subreddit_new", new_callable=AsyncMock, return_value=[]), \
         patch("backend.services.mention_monitor.fetch_comments", new_callable=AsyncMock,
               return_value={"post": post, "comments": []}), \
         patch("backend.services.mention_monitor.classify_and_store", new_callable=AsyncMock, return_value=True) as mock_classify:
        counts = await check_mentions_for_workspace(db, "ws1")

    assert counts["classified"] == 2
    called_source_ids = {c.args[2]["id"] for c in mock_classify.call_args_list}
    assert called_source_ids == {"src1", "src2"}


@pytest.mark.asyncio
async def test_budget_guard_stops_after_limit(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "max_mention_classifications_per_sweep", 1)

    db = _FakeDB()
    db.sources = [_source()]
    posts = [_post(id=f"p{i}", name=f"t3_p{i}", created_utc=1700000000 + i) for i in range(3)]

    with patch("backend.services.mention_monitor.search_mentions", new_callable=AsyncMock, return_value=posts), \
         patch("backend.services.mention_monitor.fetch_subreddit_new", new_callable=AsyncMock, return_value=[]), \
         patch("backend.services.mention_monitor.fetch_comments", new_callable=AsyncMock,
               return_value={"post": {}, "comments": []}), \
         patch("backend.services.mention_monitor.classify_and_store", new_callable=AsyncMock, return_value=True) as mock_classify:
        counts = await check_mentions_for_workspace(db, "ws1")

    assert counts["classified"] == 1
    assert mock_classify.call_count == 1


@pytest.mark.asyncio
async def test_stream_watch_skips_already_seen_and_advances_high_water_mark():
    db = _FakeDB()
    db.sources = [_source(mention_subreddits=["legaltech"])]
    db.mention_streams = [{
        "workspace_id": "ws1", "platform": "reddit", "stream_key": "r/legaltech/new", "last_seen_utc": 1000,
    }]
    old_post = _post(id="old", name="t3_old", title="Legora old", created_utc=500)
    new_post = _post(id="new", name="t3_new", title="Legora new thread", created_utc=2000)

    with patch("backend.services.mention_monitor.search_mentions", new_callable=AsyncMock, return_value=[]), \
         patch("backend.services.mention_monitor.fetch_subreddit_new", new_callable=AsyncMock,
               return_value=[old_post, new_post]), \
         patch("backend.services.mention_monitor.fetch_comments", new_callable=AsyncMock,
               return_value={"post": {}, "comments": []}), \
         patch("backend.services.mention_monitor.classify_and_store", new_callable=AsyncMock, return_value=True) as mock_classify:
        await check_mentions_for_workspace(db, "ws1")

    mock_classify.assert_called_once()  # only new_post — old_post is at/below the high-water mark
    stream_row = next(r for r in db.mention_streams if r["stream_key"] == "r/legaltech/new")
    assert stream_row["last_seen_utc"] == 2000


@pytest.mark.asyncio
async def test_source_failure_does_not_block_other_sources():
    db = _FakeDB()
    db.sources = [
        _source(id="src1", name="Legora", mention_terms=["legora"]),
        _source(id="src2", name="Leya", mention_terms=["leya"]),
    ]
    post = _post(title="Leya thread", name="t3_leya")

    async def fake_search(term, subreddit=None, limit=50):
        if term == "legora":
            raise Exception("boom")
        return [post] if term == "leya" else []

    with patch("backend.services.mention_monitor.search_mentions", side_effect=fake_search), \
         patch("backend.services.mention_monitor.fetch_subreddit_new", new_callable=AsyncMock, return_value=[]), \
         patch("backend.services.mention_monitor.fetch_comments", new_callable=AsyncMock,
               return_value={"post": post, "comments": []}), \
         patch("backend.services.mention_monitor.classify_and_store", new_callable=AsyncMock, return_value=True) as mock_classify:
        counts = await check_mentions_for_workspace(db, "ws1")

    assert counts["classified"] == 1
    mock_classify.assert_called_once()
    assert mock_classify.call_args.args[2]["id"] == "src2"
    # src1's failure means it never reaches _set_checked_at; src2 does.
    updated_ids = {u[1].get("id") for u in db.updates if u[0] == "sources"}
    assert updated_ids == {"src2"}
