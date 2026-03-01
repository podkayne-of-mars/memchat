"""FastAPI application entry point for Immortal Chat."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from src.config import get_config
from src.database import init_db
from src.routes import chat, debug, users, settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    init_db()
    yield


app = FastAPI(title="Immortal Chat", lifespan=lifespan)

# Signed cookie sessions — no passwords, just user switching
app.add_middleware(
    SessionMiddleware,
    secret_key="immortalchat-local-session-key-not-for-production",
    max_age=365 * 24 * 3600,
)

# Static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Route registration
app.include_router(chat.router)
app.include_router(debug.router)
app.include_router(users.router)
app.include_router(settings.router)


@app.get("/health")
async def health_check():
    return {"status": "ok"}


def start():
    """Entry point for running with `python -m src.main`."""
    import uvicorn
    cfg = get_config()
    uvicorn.run(
        "src.main:app",
        host=cfg.server.host,
        port=cfg.server.port,
        reload=True,
    )


if __name__ == "__main__":
    start()
