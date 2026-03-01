"""Token counting and handover threshold checking."""

import tiktoken

from src.config import get_config

# cl100k_base covers Claude's tokenisation closely enough for budgeting.
# Cache the encoder — it's expensive to initialise.
_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_text(text: str) -> int:
    """Count tokens in a plain string."""
    return len(_get_encoder().encode(text))


def count_messages(messages: list[dict], system: str | None = None) -> int:
    """Estimate token count for a full API call payload.

    Accounts for the system prompt and the per-message overhead that the
    Messages API adds (role tokens, framing).  The overhead numbers are
    approximations — close enough for budgeting, not exact billing.

    Args:
        messages: List of {"role": ..., "content": ...} dicts.
        system: Optional system prompt string.
    """
    enc = _get_encoder()
    total = 0

    # System prompt
    if system:
        # ~4 tokens overhead for the system block framing
        total += len(enc.encode(system)) + 4

    # Messages
    for msg in messages:
        # ~4 tokens per message for role + framing
        total += len(enc.encode(msg["content"])) + 4

    # Base overhead for the request structure itself
    total += 3

    return total


def check_threshold(session_input_tokens: int, session_output_tokens: int) -> bool:
    """Return True if cumulative session usage has crossed the handover threshold.

    Compares total tokens used so far against (max_context_tokens * handover_threshold).
    """
    cfg = get_config().anthropic
    limit = int(cfg.max_context_tokens * cfg.handover_threshold)
    return (session_input_tokens + session_output_tokens) >= limit


def tokens_remaining(session_input_tokens: int, session_output_tokens: int) -> int:
    """How many tokens remain before the handover threshold is hit."""
    cfg = get_config().anthropic
    limit = int(cfg.max_context_tokens * cfg.handover_threshold)
    return max(0, limit - session_input_tokens - session_output_tokens)
