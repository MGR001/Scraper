from supabase import Client, create_client

from .config import settings

_client: Client | None = None


def get_db() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.supabase_url, settings.supabase_key)
    return _client
