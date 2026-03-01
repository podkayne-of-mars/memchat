"""Chat endpoints — send messages, stream responses, retrieve history."""

import json
import uuid
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from src.anthropic_client import stream_message
from src.assembler import build_context
from src.auth import get_current_user_id, require_login
from src.counter import check_threshold
from src.curator import curate_session
from src.database import (
    create_session,
    end_session,
    get_active_session,
    get_recent_messages,
    get_user,
    save_message,
)
from src.models import ChatRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])
templates = Jinja2Templates(directory="templates")

# In-memory session token accumulators: {session_id: (input_tokens, output_tokens)}
# Reset when a session ends. Good enough for Phase 1 — single process.
_session_tokens: dict[str, tuple[int, int]] = {}


def _get_or_create_session(user_id: int) -> str:
    """Return an active session id for the user, creating one if none exists."""
    session = get_active_session(user_id)
    if session:
        return session["id"]
    session_id = str(uuid.uuid4())
    create_session(session_id, user_id)
    logger.info("New session started: %s for user %d", session_id, user_id)
    return session_id


@router.get("/")
async def root(request: Request):
    """Redirect root to /chat (or /login if not authenticated)."""
    result = require_login(request)
    if isinstance(result, RedirectResponse):
        return result
    return RedirectResponse(url="/chat", status_code=303)


@router.get("/chat")
async def chat_page(request: Request):
    """Serve the main chat UI. Requires login."""
    result = require_login(request)
    if isinstance(result, RedirectResponse):
        return result
    user = get_user(result)
    return templates.TemplateResponse(
        "chat.html", {"request": request, "user": user}
    )


@router.get("/api/messages")
async def get_messages(request: Request, limit: int = 50):
    """Return recent messages for the logged-in user."""
    user_id = get_current_user_id(request)
    if user_id is None:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return get_recent_messages(user_id, limit=limit)


@router.post("/api/chat")
async def send_message(req: ChatRequest, request: Request):
    """Send a message and stream Claude's response back as SSE.

    Flow:
    1. Ensure user + session exist
    2. Save user message to DB
    3. Build context via assembler (persona + checkpoint + buffer + new message)
    4. Stream response from Anthropic API
    5. Save complete assistant response to DB
    6. Check session token usage — trigger handover if threshold crossed
    """
    user_id = get_current_user_id(request)
    if user_id is None:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)

    user = get_user(user_id)
    if not user:
        return StreamingResponse(
            _error_stream(f"User {user_id} not found"),
            media_type="text/event-stream",
        )

    session_id = _get_or_create_session(user_id)

    # Save the user's message
    save_message(user_id, "user", req.message, session_id)

    # Build context via assembler
    system, messages = build_context(user_id, req.message)

    return StreamingResponse(
        _chat_stream(user_id, session_id, system, messages),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _chat_stream(
    user_id: int,
    session_id: str,
    system: str | None,
    messages: list[dict],
):
    """Async generator that streams SSE events to the frontend."""
    full_response: list[str] = []
    input_tokens = 0
    output_tokens = 0

    async for delta in stream_message(messages=messages, system=system):
        if delta.type == "text":
            full_response.append(delta.text)
            yield f"data: {json.dumps({'type': 'token', 'text': delta.text})}\n\n"

        elif delta.type == "error":
            yield f"data: {json.dumps({'type': 'error', 'detail': delta.text})}\n\n"
            return

        elif delta.type == "done":
            input_tokens = delta.input_tokens
            output_tokens = delta.output_tokens

    # Save the complete assistant response
    complete_text = "".join(full_response)
    if complete_text:
        msg_id = save_message(
            user_id, "assistant", complete_text, session_id,
            token_estimate=output_tokens,
        )
    else:
        msg_id = 0

    # --- Session token tracking & handover check ---
    prev_in, prev_out = _session_tokens.get(session_id, (0, 0))
    cum_in = prev_in + input_tokens
    cum_out = prev_out + output_tokens
    _session_tokens[session_id] = (cum_in, cum_out)

    handover = check_threshold(cum_in, cum_out)

    if handover:
        logger.warning(
            "HANDOVER TRIGGERED — session %s at %d tokens (in=%d, out=%d)",
            session_id, cum_in + cum_out, cum_in, cum_out,
        )

        # Run the curator to extract knowledge before ending the session
        try:
            result = await curate_session(user_id, session_id)
            logger.info(
                "Curator completed for session %s: %d entries extracted, checkpoint: %s",
                session_id,
                result.get("knowledge_count", 0),
                result.get("checkpoint_summary", "")[:100] or "(none)",
            )
        except Exception as exc:
            logger.error("Curator failed for session %s: %s", session_id, exc)

        end_session(session_id, "token_limit", tokens_used=cum_in + cum_out)
        _session_tokens.pop(session_id, None)
        # Next message will auto-create a fresh session via _get_or_create_session

    yield f"data: {json.dumps({'type': 'done', 'message_id': msg_id, 'session_id': session_id, 'input_tokens': input_tokens, 'output_tokens': output_tokens, 'handover': handover})}\n\n"


async def _error_stream(detail: str):
    """Yield a single SSE error event."""
    yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
