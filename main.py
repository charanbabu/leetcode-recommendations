import logging

from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.database import init_db
from app.routes import router
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="LeetCode AI Agent", lifespan=lifespan)
app.include_router(router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok"}
