"""Debug panel — diagnostic view of sessions, knowledge, and checkpoints."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.auth import get_current_user_id, require_login
from src.database import (
    get_active_checkpoint,
    get_active_session,
    get_connection,
    get_user,
)
from src.routes.chat import _session_tokens
from src.vector_store import retire_knowledge as vector_retire, retire_all_knowledge as vector_retire_all

logger = logging.getLogger(__name__)

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
                """SELECT id, type, topic, content, salience, event_date, status, created_at
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


@router.post("/api/debug/knowledge/{entry_id}/delete")
async def retire_knowledge(entry_id: int, request: Request):
    """Retire a single knowledge entry (sets status to 'retired')."""
    user_id = get_current_user_id(request)
    if user_id is None:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)

    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM knowledge WHERE id = ? AND user_id = ?",
            (entry_id, user_id),
        ).fetchone()
        if not row:
            return JSONResponse({"detail": "Not found"}, status_code=404)
        conn.execute(
            "UPDATE knowledge SET status = 'retired' WHERE id = ?", (entry_id,)
        )
        conn.commit()

    vector_retire(entry_id)
    logger.info("Knowledge entry %d retired by user %d", entry_id, user_id)
    return JSONResponse({"ok": True})


@router.post("/api/debug/knowledge/clear")
async def clear_all_knowledge(request: Request):
    """Retire ALL knowledge entries for the current user."""
    user_id = get_current_user_id(request)
    if user_id is None:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)

    with get_connection() as conn:
        result = conn.execute(
            "UPDATE knowledge SET status = 'retired' WHERE user_id = ? AND status = 'active'",
            (user_id,),
        )
        conn.commit()
        count = result.rowcount

    vector_retire_all(user_id)
    logger.warning("All knowledge cleared for user %d (%d entries retired)", user_id, count)
    return JSONResponse({"ok": True, "retired": count})
