"""Anthropic Messages API client with SSE streaming support."""

import json
import logging
from dataclasses import dataclass
from typing import AsyncGenerator

import httpx

from src.config import get_config

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicError(Exception):
    """Base error for Anthropic API issues."""
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class StreamDelta:
    """A chunk of streamed response."""
    type: str          # "text", "done", "error"
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


async def stream_message(
    messages: list[dict],
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 8192,
) -> AsyncGenerator[StreamDelta, None]:
    """Stream a response from the Anthropic Messages API.

    Yields StreamDelta objects:
      - type="text": a text chunk (token or group of tokens)
      - type="done": stream finished, includes final token counts
      - type="error": something went wrong

    Args:
        messages: List of {"role": "user"|"assistant", "content": "..."} dicts.
        system: Optional system prompt string.
        model: Model ID override. Defaults to config conversation_model.
        max_tokens: Max tokens in the response.
    """
    cfg = get_config().anthropic

    if not cfg.api_key:
        yield StreamDelta(type="error", text="ANTHROPIC_API_KEY not set. Add it to your environment variables.")
        return

    if model is None:
        model = cfg.conversation_model

    headers = {
        "x-api-key": cfg.api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": messages,
    }
    if system:
        body["system"] = system

    input_tokens = 0
    output_tokens = 0

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10)) as client:
            async with client.stream("POST", API_URL, headers=headers, json=body) as response:

                if response.status_code != 200:
                    # Read the error body
                    error_body = []
                    async for chunk in response.aiter_text():
                        error_body.append(chunk)
                    error_text = "".join(error_body)
                    detail = _parse_api_error(response.status_code, error_text)
                    yield StreamDelta(type="error", text=detail)
                    return

                # Parse SSE stream
                event_type = ""
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        event_type = line[7:]
                        continue

                    if not line.startswith("data: "):
                        continue

                    data_str = line[6:]
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    msg_type = data.get("type", "")

                    if msg_type == "message_start":
                        usage = data.get("message", {}).get("usage", {})
                        input_tokens = usage.get("input_tokens", 0)

                    elif msg_type == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield StreamDelta(type="text", text=delta["text"])

                    elif msg_type == "message_delta":
                        usage = data.get("usage", {})
                        output_tokens = usage.get("output_tokens", 0)

                    elif msg_type == "error":
                        err = data.get("error", {})
                        yield StreamDelta(
                            type="error",
                            text=err.get("message", "Unknown streaming error"),
                        )
                        return

        yield StreamDelta(
            type="done",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    except httpx.ConnectError:
        yield StreamDelta(type="error", text="Cannot connect to Anthropic API. Check your network connection.")
    except httpx.ReadTimeout:
        yield StreamDelta(type="error", text="Anthropic API read timeout. The response took too long.")
    except httpx.TimeoutException as exc:
        yield StreamDelta(type="error", text=f"Anthropic API timeout: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error calling Anthropic API")
        yield StreamDelta(type="error", text=f"Unexpected error: {exc}")


async def complete_message(
    messages: list[dict],
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 8192,
) -> str:
    """Non-streaming call — returns the complete response text.

    Reuses stream_message internally. Raises AnthropicError on failure.
    Used by the curator where we need the full response to parse JSON.
    """
    chunks: list[str] = []
    async for delta in stream_message(messages, system, model, max_tokens):
        if delta.type == "text":
            chunks.append(delta.text)
        elif delta.type == "error":
            raise AnthropicError(delta.text)
    return "".join(chunks)


def _parse_api_error(status_code: int, body: str) -> str:
    """Turn an HTTP error response into a human-readable message."""
    try:
        data = json.loads(body)
        msg = data.get("error", {}).get("message", body)
    except (json.JSONDecodeError, AttributeError):
        msg = body

    if status_code == 401:
        return f"Authentication failed — check your ANTHROPIC_API_KEY. ({msg})"
    if status_code == 429:
        return f"Rate limited by Anthropic. Wait a moment and try again. ({msg})"
    if status_code == 529:
        return f"Anthropic API is overloaded. Try again shortly. ({msg})"
    if status_code >= 500:
        return f"Anthropic server error ({status_code}). ({msg})"
    return f"Anthropic API error {status_code}: {msg}"
