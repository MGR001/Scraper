from fastapi import Depends, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader

from .config import settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(key: str | None = Security(_api_key_header)) -> None:
    """Reject requests that don't carry the correct X-API-Key header.

    Auth is skipped entirely when API_AUTH_KEY is not configured (empty string),
    so a fresh install works out of the box.
    """
    if not settings.api_auth_key:
        return  # auth disabled
    if key != settings.api_auth_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")
