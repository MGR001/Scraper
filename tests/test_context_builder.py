"""
build_company_context tests — Task 5 / Task 7.

Fakes out the Supabase query builder so these run offline. Covers: page_type
tier ordering, max_chars budget respected without mid-summary truncation, and
fallback to raw chunks when a source has no page_summaries yet.
"""
import pytest

from backend.routers.insights import build_company_context


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, rows):
        self._all_rows = rows
        self._filters: dict = {}
        self._in_filters: dict = {}
        self._order_key = None
        self._order_desc = False
        self._limit = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def in_(self, key, values):
        self._in_filters[key] = set(values)
        return self

    def order(self, key, desc=False):
        self._order_key = key
        self._order_desc = desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = [
            r for r in self._all_rows
            if all(r.get(k) == v for k, v in self._filters.items())
            and all(r.get(k) in v for k, v in self._in_filters.items())
        ]
        if self._order_key:
            rows = sorted(rows, key=lambda r: r.get(self._order_key) or "", reverse=self._order_desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return _FakeResult(rows)


class _FakeDB:
    def __init__(self, page_summaries=None, scraped_content=None):
        self._tables = {
            "page_summaries": page_summaries or [],
            "scraped_content": scraped_content or [],
        }

    def table(self, name):
        return _FakeQuery(self._tables.get(name, []))


SRC = {"id": "src1", "name": "Acme", "url": "https://acme.com"}


def _row(url, summary, page_type, updated_at):
    return {"source_id": SRC["id"], "url": url, "summary": summary, "page_type": page_type, "updated_at": updated_at}


@pytest.mark.asyncio
async def test_ordering_home_pricing_first():
    rows = [
        _row("https://acme.com/blog/old", "old blog post", "blog", "2026-01-01T00:00:00Z"),
        _row("https://acme.com/product", "product page", "product", "2026-01-01T00:00:00Z"),
        _row("https://acme.com/pricing", "pricing page", "pricing", "2026-01-01T00:00:00Z"),
        _row("https://acme.com/blog/new", "new blog post", "blog", "2026-06-01T00:00:00Z"),
    ]
    db = _FakeDB(page_summaries=rows)

    result = await build_company_context(db, SRC)

    pos_pricing = result.index("pricing page")
    pos_product = result.index("product page")
    pos_new_blog = result.index("new blog post")
    pos_old_blog = result.index("old blog post")

    assert pos_pricing < pos_product, "tier 0 (pricing/home) must come before tier 1 (product/solutions/customers)"
    assert pos_product < pos_new_blog, "tier 1 must come before tier 2 (the rest)"
    assert pos_new_blog < pos_old_blog, "within tier 2, more recently updated must come first"


@pytest.mark.asyncio
async def test_max_chars_respected_without_mid_summary_truncation():
    long_summary_a = "A" * 3000
    long_summary_b = "B" * 3000
    long_summary_c = "C" * 3000
    rows = [
        _row("https://acme.com/home", long_summary_a, "home", "2026-01-01T00:00:00Z"),
        _row("https://acme.com/product", long_summary_b, "product", "2026-01-01T00:00:00Z"),
        _row("https://acme.com/blog", long_summary_c, "blog", "2026-01-01T00:00:00Z"),
    ]
    db = _FakeDB(page_summaries=rows)

    result = await build_company_context(db, SRC, max_chars=4000)

    assert long_summary_a in result, "first summary must be included whole, not truncated mid-text"
    assert long_summary_b not in result, "second summary would exceed budget and must be excluded entirely"
    assert long_summary_c not in result


@pytest.mark.asyncio
async def test_empty_summaries_falls_back_to_chunks():
    chunk_rows = [
        {"source_id": SRC["id"], "content": "Some raw scraped chunk text.",
         "url": "https://acme.com/raw-page", "scraped_at": "2026-01-01T00:00:00Z"},
    ]
    db = _FakeDB(page_summaries=[], scraped_content=chunk_rows)

    result = await build_company_context(db, SRC)

    assert "Some raw scraped chunk text." in result
    assert "https://acme.com/raw-page" in result


@pytest.mark.asyncio
async def test_narrow_filter_widens_when_fewer_than_three_matches():
    rows = [
        _row("https://acme.com/pricing", "pricing page", "pricing", "2026-01-01T00:00:00Z"),
        _row("https://acme.com/careers", "careers page", "careers", "2026-01-01T00:00:00Z"),
        _row("https://acme.com/legal", "legal page", "legal", "2026-01-01T00:00:00Z"),
    ]
    db = _FakeDB(page_summaries=rows)

    # Filtering to just "pricing" would only match 1 row (< 3) — must widen to all types.
    result = await build_company_context(db, SRC, page_types=["pricing"])

    assert "pricing page" in result
    assert "careers page" in result
    assert "legal page" in result
