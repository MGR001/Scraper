"""
Page summarizer tests — Task 2 / Task 7.

Pure-function JSON parsing cases need no mocking. The hash-skip test fakes out
the DB, embedding, and completion calls so it runs offline. Run with:

    pytest tests/test_summarizer.py -v
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.summarizer import _parse_summary_response, store_page_summary


# ── JSON parsing ─────────────────────────────────────────────────────────────

def test_parse_clean_json():
    raw = json.dumps({"page_type": "pricing", "summary": "Three tiers: $10/$50/$200 per seat."})
    result = _parse_summary_response(raw)
    assert result == {"page_type": "pricing", "summary": "Three tiers: $10/$50/$200 per seat."}


def test_parse_fenced_json():
    raw = "```json\n" + json.dumps({"page_type": "product", "summary": "Ships AI drafting."}) + "\n```"
    result = _parse_summary_response(raw)
    assert result == {"page_type": "product", "summary": "Ships AI drafting."}


def test_parse_fenced_json_no_language_tag():
    raw = "```\n" + json.dumps({"page_type": "blog", "summary": "A post about pricing."}) + "\n```"
    result = _parse_summary_response(raw)
    assert result == {"page_type": "blog", "summary": "A post about pricing."}


def test_parse_garbage_falls_back_to_other():
    result = _parse_summary_response("not json at all, just prose the model wrote instead")
    assert result["page_type"] == "other"
    assert "not json at all" in result["summary"]


def test_parse_empty_falls_back():
    result = _parse_summary_response("")
    assert result["page_type"] == "other"
    assert result["summary"] == "(summary unavailable)"


def test_parse_invalid_page_type_falls_back_to_other():
    raw = json.dumps({"page_type": "totally-made-up-type", "summary": "Some real summary text."})
    result = _parse_summary_response(raw)
    assert result["page_type"] == "other"
    assert result["summary"] == "Some real summary text."


def test_parse_missing_summary_uses_raw_text():
    raw = json.dumps({"page_type": "home"})
    result = _parse_summary_response(raw)
    assert result["page_type"] == "home"
    assert result["summary"]  # falls back to the raw text, non-empty


# ── Hash-skip behaviour ──────────────────────────────────────────────────────

class _FakeQuery:
    """Minimal chainable fake for the one page_summaries access pattern
    store_page_summary uses: select().eq().eq().execute(), update().eq().execute(),
    and upsert().execute()."""

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

    def update(self, payload):
        self._mode = "update"
        self._payload = payload
        return self

    def upsert(self, record, on_conflict=None):
        self._mode = "upsert"
        self._payload = record
        return self

    def execute(self):
        if self._mode == "select":
            matched = [r for r in self._rows if all(r.get(k) == v for k, v in self._filters.items())]
            return type("Result", (), {"data": matched})()
        if self._mode == "update":
            for r in self._rows:
                if all(r.get(k) == v for k, v in self._filters.items()):
                    r.update(self._payload)
            return type("Result", (), {"data": self._rows})()
        if self._mode == "upsert":
            for r in self._rows:
                if r.get("source_id") == self._payload.get("source_id") and r.get("url") == self._payload.get("url"):
                    r.update(self._payload)
                    return type("Result", (), {"data": [r]})()
            # Simulate Postgres's `id uuid primary key default gen_random_uuid()`
            row = {"id": f"fake-id-{len(self._rows)}", **self._payload}
            self._rows.append(row)
            return type("Result", (), {"data": [row]})()
        raise AssertionError("execute() called with no operation set")


class _FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {"page_summaries": []}

    def table(self, name):
        return _FakeQuery(self.tables.setdefault(name, []))


@pytest.mark.asyncio
async def test_store_page_summary_skips_llm_call_when_content_unchanged():
    fake_db = _FakeDB()
    chunks = ["Same content, chunk one.", "Same content, chunk two."]

    with patch("backend.services.summarizer.get_service_db", return_value=fake_db), \
         patch("backend.services.summarizer._create_completion", new_callable=AsyncMock) as mock_completion, \
         patch("backend.services.summarizer.get_embedding", new_callable=AsyncMock) as mock_embedding:
        mock_completion.return_value = type("Resp", (), {
            "choices": [type("Choice", (), {
                "message": type("Msg", (), {"content": json.dumps({"page_type": "pricing", "summary": "s"})})()
            })()]
        })()
        mock_embedding.return_value = [0.0] * 1536

        first  = await store_page_summary("ws1", "src1", "https://x.com/pricing", "Pricing", chunks)
        second = await store_page_summary("ws1", "src1", "https://x.com/pricing", "Pricing", chunks)

        assert first is True, "first call must generate a summary"
        assert second is False, "second call with identical chunks must report skipped"
        assert mock_completion.call_count == 1, "second call with identical chunks must not hit the LLM"
        assert len(fake_db.tables["page_summaries"]) == 1


@pytest.mark.asyncio
async def test_store_page_summary_recalls_llm_when_content_changes():
    fake_db = _FakeDB()

    with patch("backend.services.summarizer.get_service_db", return_value=fake_db), \
         patch("backend.services.summarizer._create_completion", new_callable=AsyncMock) as mock_completion, \
         patch("backend.services.summarizer.get_embedding", new_callable=AsyncMock) as mock_embedding:
        mock_completion.return_value = type("Resp", (), {
            "choices": [type("Choice", (), {
                "message": type("Msg", (), {"content": json.dumps({"page_type": "pricing", "summary": "s"})})()
            })()]
        })()
        mock_embedding.return_value = [0.0] * 1536

        first  = await store_page_summary("ws1", "src1", "https://x.com/pricing", "Pricing", ["version one"])
        second = await store_page_summary("ws1", "src1", "https://x.com/pricing", "Pricing", ["version two, changed"])

        assert first is True and second is True
        assert mock_completion.call_count == 2, "changed content must trigger a fresh summary"
