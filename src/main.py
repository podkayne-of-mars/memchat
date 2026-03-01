"""FastAPI application entry point for memchat."""

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from src.config import get_config
from src.database import init_db
from src.vector_store import init_vector_store
from src.routes import chat, debug, users, settings

SECRET_FILE = Path("data/.session_secret")


def _get_session_secret() -> str:
    """Load session secret from file, or generate and persist a new one."""
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    SECRET_FILE.write_text(secret)
    return secret


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    init_db()
    init_vector_store()
    yield


app = FastAPI(title="memchat", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=_get_session_secret(),
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
