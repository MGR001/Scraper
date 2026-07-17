"""
Mention classifier tests — Task 3.

Parsing/enum-validation cases need no mocking. classify_and_store's dedupe
and insert paths use a minimal fake Supabase client, same pattern as
test_summarizer.py. The mandatory ~100-mention human validation pass
(precision on `relevant`, gross sentiment direction) happens separately
against real data before Task 4 wires this into automated sweeps.
"""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.mention_classifier import (
    _parse_classification,
    classify_and_store,
)


# ── JSON parsing + enum validation ──────────────────────────────────────────

def test_parse_clean_relevant_json():
    raw = json.dumps({
        "relevant": True, "confidence": 0.87, "sentiment": -0.4,
        "aspect": "support", "signal_type": "complaint",
        "is_firsthand": True, "summary": "Frustrated with slow support replies.",
    })
    result = _parse_classification(raw)
    assert result == {
        "relevant": True, "confidence": 0.87, "sentiment": -0.4,
        "aspect": "support", "signal_type": "complaint",
        "is_firsthand": True, "summary": "Frustrated with slow support replies.",
    }


def test_parse_fenced_json():
    raw = "```json\n" + json.dumps({"relevant": False}) + "\n```"
    result = _parse_classification(raw)
    assert result["relevant"] is False
    assert result["aspect"] is None


def test_parse_fenced_json_no_language_tag():
    raw = "```\n" + json.dumps({"relevant": True, "sentiment": 0.2}) + "\n```"
    result = _parse_classification(raw)
    assert result["relevant"] is True
    assert result["sentiment"] == 0.2


def test_parse_garbage_falls_back_to_relevant_none():
    result = _parse_classification("not json at all")
    assert result["relevant"] is None
    assert all(v is None for v in result.values())


def test_parse_empty_falls_back():
    result = _parse_classification("")
    assert result["relevant"] is None


def test_parse_missing_relevant_field_falls_back():
    result = _parse_classification(json.dumps({"sentiment": 0.5}))
    assert result["relevant"] is None
    assert result["sentiment"] is None  # blanked along with everything else


def test_parse_invalid_aspect_and_signal_type_fall_back_to_none():
    raw = json.dumps({
        "relevant": True, "aspect": "made-up-aspect", "signal_type": "made-up-signal",
    })
    result = _parse_classification(raw)
    assert result["relevant"] is True
    assert result["aspect"] is None
    assert result["signal_type"] is None


def test_parse_clamps_confidence_and_sentiment_out_of_range():
    raw = json.dumps({"relevant": True, "confidence": 1.5, "sentiment": -3.0})
    result = _parse_classification(raw)
    assert result["confidence"] == 1.0
    assert result["sentiment"] == -1.0


def test_parse_ignores_non_bool_is_firsthand():
    raw = json.dumps({"relevant": True, "is_firsthand": "yes"})
    result = _parse_classification(raw)
    assert result["is_firsthand"] is None


def test_parse_strips_summary_whitespace():
    raw = json.dumps({"relevant": True, "summary": "  padded summary  "})
    result = _parse_classification(raw)
    assert result["summary"] == "padded summary"


# ── classify_and_store: dedupe + insert ─────────────────────────────────────

class _FakeQuery:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._filters: dict = {}
        self._mode = None
        self._payload: dict = {}

    def select(self, *_args, **_kwargs):
        self._mode = "select"
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def insert(self, payload):
        self._mode = "insert"
        self._payload = payload
        return self

    def execute(self):
        if self._mode == "select":
            matched = [r for r in self._rows if all(r.get(k) == v for k, v in self._filters.items())]
            return type("Result", (), {"data": matched})()
        if self._mode == "insert":
            row = {"id": f"fake-{len(self._rows)}", **self._payload}
            self._rows.append(row)
            return type("Result", (), {"data": [row]})()
        raise AssertionError("execute() called with no operation set")


class _FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {"mentions": []}

    def table(self, name):
        return _FakeQuery(self.tables.setdefault(name, []))


@pytest.mark.asyncio
async def test_classify_and_store_dedupes_without_calling_llm():
    fake_db = _FakeDB()
    fake_db.tables["mentions"].append({
        "id": "existing-1", "platform": "reddit", "external_id": "t3_abc", "source_id": "src1",
    })
    source = {"id": "src1", "name": "Legora", "mention_terms": ["legora"]}
    item = {
        "name": "t3_abc", "title": "T", "selftext": "body",
        "permalink": "/r/x/comments/abc/", "created_utc": 1700000000,
        "score": 5, "author": "u", "subreddit": "legaltech",
    }

    with patch("backend.services.mention_classifier.classify_mention", new_callable=AsyncMock) as mock_classify:
        made_llm_call = await classify_and_store(fake_db, "ws1", source, item, "post")

    assert made_llm_call is False
    mock_classify.assert_not_called()
    assert len(fake_db.tables["mentions"]) == 1  # no new row inserted


@pytest.mark.asyncio
async def test_classify_and_store_inserts_new_row_and_calls_llm():
    fake_db = _FakeDB()
    source = {"id": "src1", "name": "Legora", "mention_terms": ["legora"]}
    item = {
        "name": "t3_new", "title": "Anyone tried Legora?", "selftext": "curious",
        "permalink": "/r/legaltech/comments/new/x/", "created_utc": 1700000000,
        "score": 5, "author": "u1", "subreddit": "legaltech",
    }

    with patch("backend.services.mention_classifier.classify_mention", new_callable=AsyncMock) as mock_classify:
        mock_classify.return_value = {
            "relevant": True, "confidence": 0.9, "sentiment": 0.5, "aspect": "product",
            "signal_type": "praise", "is_firsthand": True, "summary": "Likes it.",
        }
        made_llm_call = await classify_and_store(fake_db, "ws1", source, item, "post")

    assert made_llm_call is True
    mock_classify.assert_called_once()
    assert len(fake_db.tables["mentions"]) == 1

    row = fake_db.tables["mentions"][0]
    assert row["url"] == "https://www.reddit.com/r/legaltech/comments/new/x/"
    assert row["relevant"] is True
    assert row["kind"] == "post"
    assert row["published_at"] == datetime.fromtimestamp(1700000000, tz=timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_classify_and_store_uses_comment_body_when_kind_is_comment():
    fake_db = _FakeDB()
    source = {"id": "src1", "name": "Legora", "mention_terms": ["legora"]}
    item = {
        "name": "t1_new", "title": "Thread title", "body": "same here, switched last month",
        "selftext": "should not be used for comments",
        "permalink": "/r/legaltech/comments/x/_/new/", "created_utc": 1700000000,
        "score": 2, "author": "u2", "subreddit": "legaltech",
    }

    with patch("backend.services.mention_classifier.classify_mention", new_callable=AsyncMock) as mock_classify:
        mock_classify.return_value = {
            "relevant": True, "confidence": 0.7, "sentiment": -0.1, "aspect": "other",
            "signal_type": "switching_intent", "is_firsthand": True, "summary": "Switched away.",
        }
        await classify_and_store(fake_db, "ws1", source, item, "comment")

    called_args = mock_classify.call_args.args
    assert called_args[2] == "Thread title"       # thread_title
    assert called_args[3] == "same here, switched last month"  # body
    assert called_args[4] is True                 # is_comment

    row = fake_db.tables["mentions"][0]
    assert row["body"] == "same here, switched last month"
    assert row["kind"] == "comment"
