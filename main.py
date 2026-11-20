import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from app.config import settings
from app.database import init_db
from app.routes import router
from app.scheduler import auto_seed_problems, start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    if settings.auto_seed_on_startup:
        await auto_seed_problems()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="LeetCode AI Agent", lifespan=lifespan)
app.include_router(router, prefix="/api")

STATIC_DIR = Path(__file__).parent / "app" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/connect-leetcode")
async def connect_page():
    """Serve the LeetCode Premium connect page."""
    return FileResponse(str(STATIC_DIR / "connect.html"))
