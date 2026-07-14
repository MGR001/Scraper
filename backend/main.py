import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse

from .routers import insights, scraper, sources
from .routers.auth_router import router as auth_router
from .routers.workspaces import router as workspaces_router
from .routers.settings import router as settings_router
from .scheduler import start_scheduler, stop_scheduler

_FRONTEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()
    from .services.scraper import close_http_client
    await close_http_client()


app = FastAPI(title="RIvals", lifespan=lifespan)

# Auth is per-endpoint via Depends(get_workspace) / Depends(get_current_user)
app.include_router(sources.router,    prefix="/api/sources",    tags=["sources"])
app.include_router(scraper.router,    prefix="/api/scraper",    tags=["scraper"])
app.include_router(insights.router,   prefix="/api/insights",   tags=["insights"])
app.include_router(auth_router,       prefix="/api",            tags=["auth"])
app.include_router(workspaces_router, prefix="/api/workspaces", tags=["workspaces"])
app.include_router(settings_router,   prefix="/api/settings",   tags=["settings"])


@app.get("/health", include_in_schema=False)
async def health():
    return {"ok": True}


_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(os.path.join(_FRONTEND, "home.html"), headers=_NO_CACHE)


@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(os.path.join(_FRONTEND, "login.html"), headers=_NO_CACHE)


@app.get("/app", include_in_schema=False)
async def app_page():
    return FileResponse(os.path.join(_FRONTEND, "index.html"), headers=_NO_CACHE)


@app.get("/app.js", include_in_schema=False)
async def app_js():
    return FileResponse(
        os.path.join(_FRONTEND, "app.js"), media_type="application/javascript", headers=_NO_CACHE
    )
