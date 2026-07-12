"""
Tenancy isolation tests — Task 11.

These tests assert that workspace A's data is never visible to workspace B,
for every data endpoint in the API. Run with:

    pytest tests/test_tenancy.py -v

Prerequisites:
  - Two Supabase users must exist in the test project, with their JWTs set
    in the environment:
      TENANT_A_TOKEN — a user who belongs only to workspace A
      TENANT_B_TOKEN — a user who belongs only to workspace B
      TENANT_A_WORKSPACE_ID
      TENANT_B_WORKSPACE_ID
  - The server must be running at TEST_BASE_URL (default: http://localhost:8000)
"""
import os
import pytest
import httpx

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


# ── Helpers ────────────────────────────────────────────────────────────────────

@skip_if_no_tokens
def test_unauthenticated_request_returns_401():
    r = httpx.get(f"{BASE_URL}/api/sources/")
    assert r.status_code == 401, f"Expected 401, got {r.status_code}"


@skip_if_no_tokens
def test_wrong_workspace_returns_403():
    """User A cannot access workspace B's data."""
    r = httpx.get(
        f"{BASE_URL}/api/sources/",
        headers=headers(TOKEN_A, WS_B),  # A's token, B's workspace
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


@skip_if_no_tokens
def test_sources_isolation():
    """Sources from workspace A are not visible to workspace B."""
    # Create a source in workspace A
    create_r = httpx.post(
        f"{BASE_URL}/api/sources/",
        json={"name": "TenancyTest", "url": "https://tenancy-test-a.example.com",
              "category": "general", "scrape_interval": 24},
        headers=headers(TOKEN_A, WS_A),
    )
    assert create_r.status_code == 201, f"Create failed: {create_r.text}"
    source_id = create_r.json()["id"]

    try:
        # A can see their own source
        list_a = httpx.get(f"{BASE_URL}/api/sources/", headers=headers(TOKEN_A, WS_A))
        assert list_a.status_code == 200
        ids_a = [s["id"] for s in list_a.json()]
        assert source_id in ids_a, "Workspace A cannot see its own source"

        # B cannot see A's source
        list_b = httpx.get(f"{BASE_URL}/api/sources/", headers=headers(TOKEN_B, WS_B))
        assert list_b.status_code == 200
        ids_b = [s["id"] for s in list_b.json()]
        assert source_id not in ids_b, "Workspace B can see workspace A's source — TENANCY LEAK"

        # B cannot access A's source directly by ID
        direct_b = httpx.put(
            f"{BASE_URL}/api/sources/{source_id}",
            json={"name": "Hijacked"},
            headers=headers(TOKEN_B, WS_B),
        )
        assert direct_b.status_code in (403, 404), \
            f"B could mutate A's source: {direct_b.status_code} {direct_b.text}"

    finally:
        # Cleanup
        httpx.delete(f"{BASE_URL}/api/sources/{source_id}", headers=headers(TOKEN_A, WS_A))


@skip_if_no_tokens
def test_me_returns_only_own_workspaces():
    """GET /api/me returns only the caller's workspaces."""
    r_a = httpx.get(f"{BASE_URL}/api/me", headers={"Authorization": f"Bearer {TOKEN_A}"})
    assert r_a.status_code == 200
    ws_ids_a = [w["id"] for w in r_a.json().get("workspaces", [])]
    assert WS_A in ws_ids_a
    assert WS_B not in ws_ids_a, "Workspace B visible in A's /api/me — TENANCY LEAK"

    r_b = httpx.get(f"{BASE_URL}/api/me", headers={"Authorization": f"Bearer {TOKEN_B}"})
    assert r_b.status_code == 200
    ws_ids_b = [w["id"] for w in r_b.json().get("workspaces", [])]
    assert WS_B in ws_ids_b
    assert WS_A not in ws_ids_b, "Workspace A visible in B's /api/me — TENANCY LEAK"


@skip_if_no_tokens
def test_workspace_detail_denied_to_non_member():
    """Workspace A's detail is not accessible by user B."""
    r = httpx.get(f"{BASE_URL}/api/workspaces/{WS_A}", headers=headers(TOKEN_B, WS_B))
    assert r.status_code in (403, 404), \
        f"User B accessed workspace A detail: {r.status_code} {r.text}"


@skip_if_no_tokens
def test_service_key_not_in_frontend(tmp_path):
    """Confirm no service-role key appears in any frontend file."""
    service_key = os.getenv("SUPABASE_KEY", "")
    if not service_key or len(service_key) < 20:
        pytest.skip("SUPABASE_KEY not set or too short to search for")

    frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
    for fname in os.listdir(frontend_dir):
        fpath = os.path.join(frontend_dir, fname)
        if not os.path.isfile(fpath):
            continue
        content = open(fpath, encoding="utf-8", errors="ignore").read()
        assert service_key not in content, \
            f"Service-role key found in frontend/{fname} — KEY LEAK"
