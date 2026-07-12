from supabase import Client, create_client

from .config import settings

# ── Service-role client (scheduler / background jobs only) ──
# This client bypasses RLS — never import it into a router.
_service_client: Client | None = None


def get_service_db() -> Client:
    """Return the service-role Supabase client (bypasses RLS).
    Only use in background jobs (scheduler) that have no user JWT."""
    global _service_client
    if _service_client is None:
        _service_client = create_client(settings.supabase_url, settings.supabase_key)
    return _service_client


# Legacy alias used by any code that predates multi-tenancy.
# Callers that still use this get the service client; migrate them to
# get_user_db() as part of Task 4.
def get_db() -> Client:
    return get_service_db()


def get_user_db(access_token: str) -> Client:
    """Return an anon-key Supabase client authenticated as the calling user.
    All queries through this client run under RLS with the user's identity."""
    anon_key = settings.supabase_anon_key or settings.supabase_key
    client = create_client(settings.supabase_url, anon_key)
    client.postgrest.auth(access_token)
    return client
