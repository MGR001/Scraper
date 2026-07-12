import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.responses import FileResponse

from .auth import require_api_key
from .routers import insights, scraper, sources
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

_auth = [Depends(require_api_key)]

app.include_router(sources.router, prefix="/api/sources", tags=["sources"], dependencies=_auth)
app.include_router(scraper.router, prefix="/api/scraper", tags=["scraper"], dependencies=_auth)
app.include_router(insights.router, prefix="/api/insights", tags=["insights"], dependencies=_auth)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(_FRONTEND, "index.html"))


@app.get("/app.js", include_in_schema=False)
async def app_js():
    return FileResponse(os.path.join(_FRONTEND, "app.js"), media_type="application/javascript")
