"""Debug panel — diagnostic view of sessions, knowledge, and checkpoints."""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from src.auth import require_login
from src.database import (
    get_active_checkpoint,
    get_active_session,
    get_connection,
    get_user,
)
from src.routes.chat import _session_tokens

router = APIRouter(tags=["debug"])
templates = Jinja2Templates(directory="templates")


@router.get("/debug")
async def debug_page(request: Request):
    """Render the debug panel with all diagnostic data for the logged-in user."""
    result = require_login(request)
    if isinstance(result, RedirectResponse):
        return result
    user_id = result
    user = get_user(user_id)

    # Active session + estimated tokens
    active_session = get_active_session(user_id)
    session_id = active_session["id"] if active_session else None
    token_in, token_out = _session_tokens.get(session_id, (0, 0)) if session_id else (0, 0)

    # All knowledge entries for this user (all statuses, not just active)
    with get_connection() as conn:
        knowledge = [
            dict(r) for r in conn.execute(
                """SELECT id, type, topic, content, confidence, status, created_at
                   FROM knowledge WHERE user_id = ?
                   ORDER BY created_at DESC""",
                (user_id,),
            ).fetchall()
        ]

    # Current checkpoint
    checkpoint = get_active_checkpoint(user_id)

    # Session history
    with get_connection() as conn:
        sessions = [
            dict(r) for r in conn.execute(
                """SELECT id, started_at, ended_at, end_reason, tokens_used
                   FROM sessions WHERE user_id = ?
                   ORDER BY started_at DESC""",
                (user_id,),
            ).fetchall()
        ]

    return templates.TemplateResponse("debug.html", {
        "request": request,
        "user": user,
        "user_id": user_id,
        "session_id": session_id,
        "token_in": token_in,
        "token_out": token_out,
        "token_total": token_in + token_out,
        "knowledge": knowledge,
        "checkpoint": checkpoint,
        "sessions": sessions,
    })
