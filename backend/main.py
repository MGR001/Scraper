import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse

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


app = FastAPI(title="StrategyHub", lifespan=lifespan)

app.include_router(sources.router, prefix="/api/sources", tags=["sources"])
app.include_router(scraper.router, prefix="/api/scraper", tags=["scraper"])
app.include_router(insights.router, prefix="/api/insights", tags=["insights"])


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(_FRONTEND, "index.html"))
