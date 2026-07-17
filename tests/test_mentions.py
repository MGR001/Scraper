"""
Mentions guard-rail tests — Task 6.

The aggregation math (weighted sentiment, low-n suppression, spike
detection) is pure and tested directly, no mocking needed. Tenancy
isolation and the DB-level unique constraint need a live server + two
real tenant users, so those follow tests/test_tenancy.py's existing
pattern: skipped unless TENANT_A_TOKEN/TENANT_B_TOKEN/
TENANT_A_WORKSPACE_ID/TENANT_B_WORKSPACE_ID are set in the environment.
"""
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.routers.mentions import _is_spike, _weighted_sentiment, summarize_source_mentions

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8000")
TOKEN_A  = os.getenv("TENANT_A_TOKEN")
TOKEN_B  = os.getenv("TENANT_B_TOKEN")
WS_A     = os.getenv("TENANT_A_WORKSPACE_ID")
WS_B     = os.getenv("TENANT_B_WORKSPACE_ID")

skip_if_no_tokens = pytest.mark.skipif(
    not (TOKEN_A and TOKEN_B and WS_A and WS_B),
    reason="TENANT_A_TOKEN, TENANT_B_TOKEN, TENANT_A_WORKSPACE_ID, TENANT_B_WORKSPACE_ID must be set"
)


def headers(token: str, workspace_id: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-Workspace-Id": workspace_id,
        "Content-Type": "application/json",
    }


def _mention(sentiment=None, score=0, signal_type=None, aspect=None,
             published_at=None, fetched_at=None):
    return {
        "sentiment": sentiment, "score": score, "signal_type": signal_type,
        "aspect": aspect, "published_at": published_at, "fetched_at": fetched_at,
    }


# ── Weighted-sentiment math ──────────────────────────────────────────────────

def test_weighted_sentiment_weights_by_log_score():
    import math
    rows = [
        _mention(sentiment=1.0, score=100),   # weight = ln(101)
        _mention(sentiment=-1.0, score=0),    # weight = ln(1) = 0 -> contributes nothing
    ]
    result = _weighted_sentiment(rows)
    # The zero-score row has zero weight, so the result is entirely the +1.0 row.
    assert result == pytest.approx(1.0)
    assert math.log(1 + 100) > 0  # sanity check on the weighting assumption


def test_weighted_sentiment_falls_back_to_plain_average_when_all_scores_zero():
    rows = [_mention(sentiment=0.5, score=0), _mention(sentiment=-0.5, score=0)]
    # Every row has zero weight (ln(1)=0); a naive weighted average would be
    # 0/0. Real sentiment data exists here and must not be thrown away.
    assert _weighted_sentiment(rows) == pytest.approx(0.0)


def test_weighted_sentiment_ignores_null_sentiment_rows():
    rows = [_mention(sentiment=None, score=50), _mention(sentiment=0.8, score=10)]
    assert _weighted_sentiment(rows) == pytest.approx(0.8)


def test_weighted_sentiment_none_when_no_sentiment_data_at_all():
    rows = [_mention(sentiment=None, score=10), _mention(sentiment=None, score=0)]
    assert _weighted_sentiment(rows) is None


# ── Low-n suppression ─────────────────────────────────────────────────────────

def test_summarize_source_suppresses_below_five_relevant_mentions():
    source = {"id": "src1", "name": "Legora"}
    rows = [_mention(sentiment=0.5, score=1) for _ in range(4)]
    result = summarize_source_mentions(source, rows)
    assert result == {"source_id": "src1", "source_name": "Legora", "n": 4, "insufficient": True}


def test_summarize_source_no_data_distinct_from_zero_sentiment():
    """A source with zero mentions and a source with mixed-to-neutral
    sentiment must never look the same — both would otherwise render as
    'insufficient' or 0.0, but for different reasons the UI must convey."""
    source = {"id": "src1", "name": "Legora"}
    no_data = summarize_source_mentions(source, [])
    assert no_data["insufficient"] is True
    assert no_data["n"] == 0

    exactly_neutral = summarize_source_mentions(source, [
        _mention(sentiment=0.0, score=10) for _ in range(5)
    ])
    assert exactly_neutral["insufficient"] is False
    assert exactly_neutral["weighted_sentiment"] == pytest.approx(0.0)


def test_summarize_source_returns_full_stats_at_five_relevant_mentions():
    source = {"id": "src1", "name": "Legora"}
    rows = [
        _mention(sentiment=0.5, score=10, signal_type="praise"),
        _mention(sentiment=-0.6, score=10, signal_type="complaint", aspect="support"),
        _mention(sentiment=-0.4, score=5, signal_type="complaint", aspect="support"),
        _mention(sentiment=-0.2, score=5, signal_type="switching_intent", aspect="pricing"),
        _mention(sentiment=0.1, score=1, signal_type="question"),
    ]
    result = summarize_source_mentions(source, rows)
    assert result["insufficient"] is False
    assert result["n"] == 5
    assert result["switching_intent_count"] == 1
    assert result["top_negative_aspect"] == "support"  # 2 complaints vs 1 pricing


# ── Spike detection ───────────────────────────────────────────────────────────

def test_spike_true_when_24h_volume_far_exceeds_trailing_average():
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    rows = [_mention(published_at=(now - timedelta(hours=h)).isoformat()) for h in range(6)]
    # 6 mentions in the last 24h, zero in the trailing 7 days before that.
    assert _is_spike(rows, now=now) is True


def test_spike_false_below_minimum_volume():
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    rows = [_mention(published_at=(now - timedelta(hours=h)).isoformat()) for h in range(3)]
    # Only 3 in the last 24h — below the minimum, even with zero baseline.
    assert _is_spike(rows, now=now) is False


def test_spike_false_when_volume_matches_trailing_average():
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    recent = [_mention(published_at=(now - timedelta(hours=h)).isoformat()) for h in range(6)]
    # 42 mentions spread over the trailing 7 days = 6/day, matching the 6 seen today.
    trailing = [
        _mention(published_at=(now - timedelta(days=1, hours=h)).isoformat())
        for h in range(42)
    ]
    assert _is_spike(recent + trailing, now=now) is False


def test_spike_uses_fetched_at_when_published_at_missing():
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    rows = [_mention(published_at=None, fetched_at=(now - timedelta(hours=h)).isoformat())
            for h in range(6)]
    assert _is_spike(rows, now=now) is True


# ── Tenancy isolation (live server required) ─────────────────────────────────

@skip_if_no_tokens
def test_mentions_feed_isolated_by_workspace():
    list_a = httpx.get(f"{BASE_URL}/api/mentions/?only_relevant=false", headers=headers(TOKEN_A, WS_A))
    assert list_a.status_code == 200
    list_b = httpx.get(f"{BASE_URL}/api/mentions/?only_relevant=false", headers=headers(TOKEN_B, WS_B))
    assert list_b.status_code == 200

    ids_a = {m["id"] for m in list_a.json()}
    ids_b = {m["id"] for m in list_b.json()}
    assert not (ids_a & ids_b), "Workspace A and B share mention rows — TENANCY LEAK"


@skip_if_no_tokens
def test_mentions_summary_isolated_by_workspace():
    summary_a = httpx.get(f"{BASE_URL}/api/mentions/summary", headers=headers(TOKEN_A, WS_A))
    assert summary_a.status_code == 200
    source_ids_a = {r["source_id"] for r in summary_a.json()["results"]}

    summary_b = httpx.get(f"{BASE_URL}/api/mentions/summary", headers=headers(TOKEN_B, WS_B))
    assert summary_b.status_code == 200
    source_ids_b = {r["source_id"] for r in summary_b.json()["results"]}

    assert not (source_ids_a & source_ids_b), \
        "Workspace A and B share source_ids in mentions summary — TENANCY LEAK"


# ── Dedupe on the DB unique constraint (live server + service DB required) ───

@skip_if_no_tokens
def test_mentions_unique_constraint_rejects_duplicate_insert():
    """Belt-and-braces check: classify_and_store's app-level dedupe (tested in
    test_mention_classifier.py) is backed by a real DB constraint, not just
    application logic that could be bypassed by a future caller."""
    from backend.database import get_service_db

    create_r = httpx.post(
        f"{BASE_URL}/api/sources/",
        json={"name": "MentionsDedupeTest", "url": "https://mentions-dedupe-test.example.com",
              "category": "competitor", "scrape_interval": 24},
        headers=headers(TOKEN_A, WS_A),
    )
    assert create_r.status_code == 201, f"Create failed: {create_r.text}"
    source_id = create_r.json()["id"]
    db = get_service_db()

    row = {
        "workspace_id": WS_A, "source_id": source_id, "platform": "reddit",
        "external_id": "t3_dedupe_test", "kind": "post", "url": "https://reddit.com/x",
    }
    try:
        db.table("mentions").insert(row).execute()
        with pytest.raises(Exception):
            db.table("mentions").insert(row).execute()
    finally:
        db.table("mentions").delete().eq("source_id", source_id).execute()
        httpx.delete(f"{BASE_URL}/api/sources/{source_id}", headers=headers(TOKEN_A, WS_A))
