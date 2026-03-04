"""Chat endpoints — send messages, stream responses, retrieve history."""

import asyncio
import json
import uuid
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from src.anthropic_client import stream_message
from src.assembler import build_context
from src.auth import get_current_user_id, require_login
from src.config import get_config
from src.counter import check_threshold
from src.curator import curate_session
from src.transcript import save_transcript
from src.database import (
    create_session,
    end_session,
    get_active_session,
    get_all_active_sessions,
    get_recent_messages,
    get_user,
    save_message,
)
from src.models import ChatRequest
from src.file_read import read_file
from src.url_fetch import fetch_url

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])
templates = Jinja2Templates(directory="templates")

# In-memory session token accumulators: {session_id: (input_tokens, output_tokens)}
# Reset when a session ends. Good enough for Phase 1 — single process.
_session_tokens: dict[str, tuple[int, int]] = {}

# Hold references to background curator tasks so they don't get GC'd mid-flight.
_background_tasks: set[asyncio.Task] = set()


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
    save_message(
        user_id, "user", req.message, session_id,
        image_data=req.image_data,
        image_media_type=req.image_media_type,
    )

    # Build context via assembler
    try:
        system, messages = build_context(
            user_id, req.message,
            image_data=req.image_data,
            image_media_type=req.image_media_type,
        )
    except Exception as exc:
        logger.exception("build_context failed")
        return StreamingResponse(
            _error_stream(f"Context build failed: {exc}"),
            media_type="text/event-stream",
        )

    return StreamingResponse(
        _chat_stream(user_id, session_id, system, messages),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _build_tools() -> list[dict] | None:
    """Build the tools list from config."""
    cfg = get_config().anthropic
    tools = []
    if cfg.web_search:
        tools.append({
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": cfg.web_search_max_uses,
        })
        logger.info("Web search enabled (max_uses=%d)", cfg.web_search_max_uses)
    if cfg.url_fetch:
        tools.append({
            "name": "fetch_url",
            "description": (
                "Fetch and read the contents of a web page URL. Use this when "
                "the user shares a URL or when you want to read a page found "
                "via web search. Returns the page text content."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch",
                    }
                },
                "required": ["url"],
            },
        })
        logger.info("URL fetch tool enabled")
    tools.append({
        "name": "read_file",
        "description": (
            "Read the contents of a local file or list a directory. Use this "
            "when the user asks about code, config, or any file. Takes an "
            "absolute path or a path relative to the working directory. "
            "For large files, use from_line/to_line to read only a range of "
            "lines (0-based, inclusive) instead of the entire file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative file/directory path",
                },
                "from_line": {
                    "type": "integer",
                    "description": "First line to return (0-based). Omit to start from the beginning.",
                },
                "to_line": {
                    "type": "integer",
                    "description": "Last line to return (0-based, inclusive). Omit to read to the end.",
                },
            },
            "required": ["path"],
        },
    })
    logger.info("File read tool enabled")
    return tools or None


async def _chat_stream(
    user_id: int,
    session_id: str,
    system: str | None,
    messages: list[dict],
):
    """Async generator that streams SSE events to the frontend.

    Supports multi-turn tool use: when the model calls a client-side tool
    (e.g. fetch_url), we execute it locally, send the result back, and let
    the model continue. Capped at MAX_TOOL_LOOPS iterations.
    """
    MAX_TOOL_LOOPS = 5

    full_response: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0

    tools = _build_tools()

    for loop_iter in range(MAX_TOOL_LOOPS):
        # Track content blocks for this API call
        assistant_content_blocks: list[dict] = []
        current_text_parts: list[str] = []
        pending_tool_uses: list[dict] = []
        input_tokens = 0
        output_tokens = 0
        stop_reason = ""

        async for delta in stream_message(messages=messages, system=system, tools=tools):
            if delta.type == "text":
                current_text_parts.append(delta.text)
                full_response.append(delta.text)
                yield f"data: {json.dumps({'type': 'token', 'text': delta.text})}\n\n"

            elif delta.type == "web_search":
                yield f"data: {json.dumps({'type': 'web_search'})}\n\n"

            elif delta.type == "tool_use":
                # Flush any accumulated text into a content block
                if current_text_parts:
                    assistant_content_blocks.append({
                        "type": "text",
                        "text": "".join(current_text_parts),
                    })
                    current_text_parts = []
                # Record the tool_use block
                tool_block = {
                    "type": "tool_use",
                    "id": delta.tool_use_id,
                    "name": delta.tool_name,
                    "input": delta.tool_input or {},
                }
                assistant_content_blocks.append(tool_block)
                pending_tool_uses.append(tool_block)

            elif delta.type == "error":
                yield f"data: {json.dumps({'type': 'error', 'detail': delta.text})}\n\n"
                return

            elif delta.type == "done":
                input_tokens = delta.input_tokens
                output_tokens = delta.output_tokens
                stop_reason = delta.stop_reason

        total_input_tokens += input_tokens
        total_output_tokens += output_tokens

        # Flush remaining text
        if current_text_parts:
            assistant_content_blocks.append({
                "type": "text",
                "text": "".join(current_text_parts),
            })
            current_text_parts = []

        # Only continue the loop if the model explicitly asked for tool results
        # (stop_reason == "tool_use") AND we have tool calls to execute.
        # Web search responses may include content blocks that look like tool_use
        # but have stop_reason "end_turn" — those should not re-trigger the loop.
        if stop_reason != "tool_use" or not pending_tool_uses:
            break

        # --- Execute tool calls and loop ---
        # Append the assistant's full response (text + tool_use blocks) to messages
        messages.append({"role": "assistant", "content": assistant_content_blocks})

        # Execute each tool and build tool_result blocks
        tool_results = []
        for tool in pending_tool_uses:
            if tool["name"] == "fetch_url":
                url = tool["input"].get("url", "")
                logger.info("Fetching URL: %s", url)
                yield f"data: {json.dumps({'type': 'fetching_url', 'url': url})}\n\n"
                result_text = await fetch_url(url)
            elif tool["name"] == "read_file":
                file_path = tool["input"].get("path", "")
                from_line = tool["input"].get("from_line")
                to_line = tool["input"].get("to_line")
                logger.info("Reading file: %s (lines %s–%s)", file_path, from_line, to_line)
                yield f"data: {json.dumps({'type': 'reading_file', 'path': file_path})}\n\n"
                result_text = read_file(file_path, from_line=from_line, to_line=to_line)
            else:
                result_text = f"Unknown tool: {tool['name']}"
                logger.warning("Unknown tool called: %s", tool["name"])

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool["id"],
                "content": result_text,
            })

        # Append tool results as a user message
        messages.append({"role": "user", "content": tool_results})
        logger.info("Tool loop iteration %d — %d tool(s) executed, continuing", loop_iter + 1, len(tool_results))
    else:
        # Hit max loop iterations
        logger.warning("Tool loop hit max iterations (%d) for session %s", MAX_TOOL_LOOPS, session_id)

    # Save the complete assistant response (all text across all loop iterations)
    complete_text = "".join(full_response)
    if complete_text:
        msg_id = save_message(
            user_id, "assistant", complete_text, session_id,
            token_estimate=total_output_tokens,
        )
    else:
        msg_id = 0

    # --- Session token tracking & handover check ---
    prev_in, prev_out = _session_tokens.get(session_id, (0, 0))
    cum_in = prev_in + total_input_tokens
    cum_out = prev_out + total_output_tokens
    _session_tokens[session_id] = (cum_in, cum_out)

    handover = check_threshold(cum_in, cum_out)

    if handover:
        logger.warning(
            "HANDOVER TRIGGERED — session %s at %d tokens (in=%d, out=%d)",
            session_id, cum_in + cum_out, cum_in, cum_out,
        )

        # Save transcript synchronously — fast disk I/O, needed before curation
        transcript_file = save_transcript(session_id)

        # End session immediately so the next message gets a fresh one
        end_session(session_id, "token_limit", tokens_used=cum_in + cum_out)
        _session_tokens.pop(session_id, None)

        # Curator runs in the background — don't block the user
        task = asyncio.create_task(_run_curator(user_id, session_id, transcript_file))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    yield f"data: {json.dumps({'type': 'done', 'message_id': msg_id, 'session_id': session_id, 'input_tokens': total_input_tokens, 'output_tokens': total_output_tokens, 'handover': handover})}\n\n"


async def _run_curator(user_id: int, session_id: str, transcript_file: str | None) -> None:
    """Run the curator as a background task so the UI isn't blocked."""
    try:
        result = await curate_session(user_id, session_id, transcript_file=transcript_file)
        logger.info(
            "Curator completed for session %s: %d entries extracted, checkpoint: %s",
            session_id,
            result.get("knowledge_count", 0),
            result.get("checkpoint_summary", "")[:100] or "(none)",
        )
    except Exception as exc:
        logger.error("Curator failed for session %s: %s", session_id, exc)


async def graceful_shutdown() -> None:
    """Save transcripts and curate all active sessions before the process exits.

    Called from the lifespan handler on shutdown. Also waits for any
    background curator tasks that are already in flight.
    """
    # 1. Process any active sessions that haven't been curated yet
    active = get_all_active_sessions()
    if active:
        logger.info("Graceful shutdown: processing %d active session(s)", len(active))
    for session in active:
        sid = session["id"]
        uid = session["user_id"]
        try:
            transcript_file = save_transcript(sid)
            end_session(sid, "manual")
            _session_tokens.pop(sid, None)
            result = await curate_session(uid, sid, transcript_file=transcript_file)
            logger.info(
                "Shutdown curator for session %s: %d entries",
                sid, result.get("knowledge_count", 0),
            )
        except Exception as exc:
            logger.error("Shutdown curator failed for session %s: %s", sid, exc)

    # 2. Wait for any background curator tasks already running
    if _background_tasks:
        logger.info("Graceful shutdown: waiting for %d background task(s)", len(_background_tasks))
        await asyncio.gather(*_background_tasks, return_exceptions=True)

    logger.info("Graceful shutdown complete.")


async def _error_stream(detail: str):
    """Yield a single SSE error event."""
    yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
