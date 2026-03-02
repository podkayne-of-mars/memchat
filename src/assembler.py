"""Context assembler — builds the API payload for each message.

Priority order (spec §2):
  1. System prompt — hardcoded operating instructions, always first
  2. Persona — user-editable personality/style, non-negotiable
  3. Checkpoint — always, if one exists
  4. Knowledge entries — relevant entries from knowledge store
  5. Conversation buffer — last N messages, most recent first fill
  6. New user message — always last

Token budget: system prompt + persona + checkpoint + new message are non-negotiable.
Remaining budget is shared between knowledge and conversation buffer.
Knowledge is filled first (higher priority), buffer gets what's left.
"""

import logging

from src.config import get_config
from src.counter import count_text
from src.database import (
    get_active_checkpoint,
    get_active_persona,
    get_recent_messages,
)
from src.knowledge import retrieve_knowledge, format_knowledge_block
from src.system_prompt import SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def build_context(
    user_id: int,
    new_message: str,
    image_data: str | None = None,
    image_media_type: str | None = None,
) -> tuple[str | None, list[dict]]:
    """Assemble the full context for an API call.

    Returns (system_prompt, messages_list) ready to pass to the Anthropic client.
    The system prompt combines: system instructions + persona + checkpoint + knowledge.
    The messages list contains the conversation buffer + new user message.
    """
    cfg = get_config()
    max_input = int(cfg.anthropic.max_context_tokens * cfg.anthropic.handover_threshold)

    # --- 1. System prompt (hardcoded, non-negotiable) ---
    # Always first — core operating instructions

    # --- 2. Persona (user-editable, non-negotiable) ---
    persona = get_active_persona(user_id)
    persona_text = persona["persona_text"] if persona else None

    # --- 3. Checkpoint (non-negotiable if exists) ---
    checkpoint = get_active_checkpoint(user_id)
    checkpoint_text = None
    if checkpoint:
        cp_date = (checkpoint.get("created_at") or "")[:10]
        date_suffix = f" - as of {cp_date}" if cp_date else ""
        checkpoint_text = f"[Current conversation state{date_suffix}]\n{checkpoint['summary']}"
        topics = checkpoint.get("active_topics")
        if topics:
            checkpoint_text += f"\n[Active topics: {topics}]"

    # --- Account for non-negotiable tokens ---
    # Build the base system prompt (system + persona + checkpoint) to count it
    base_system_parts = [SYSTEM_PROMPT]
    if persona_text:
        base_system_parts.append(persona_text)
    if checkpoint_text:
        base_system_parts.append(checkpoint_text)
    base_system = "\n\n".join(base_system_parts)

    used = 0
    if base_system:
        used += count_text(base_system) + 4  # system framing overhead

    # New message is non-negotiable
    new_msg_tokens = count_text(new_message) + 4
    used += new_msg_tokens
    used += 3  # base request overhead

    remaining = max_input - used

    # --- 4. Knowledge entries — fill from remaining budget ---
    entries = retrieve_knowledge(user_id, new_message)
    knowledge_block = ""
    knowledge_tokens = 0

    if entries:
        full_block = format_knowledge_block(entries)
        full_block_tokens = count_text(full_block)

        if full_block_tokens <= remaining:
            # All entries fit
            knowledge_block = full_block
            knowledge_tokens = full_block_tokens
        else:
            # Fit as many entries as we can (they're already relevance-ordered)
            knowledge_block = _fit_knowledge_to_budget(entries, remaining)
            knowledge_tokens = count_text(knowledge_block) if knowledge_block else 0

    # Build the final system prompt: system instructions + persona + checkpoint + knowledge
    system_parts = [SYSTEM_PROMPT]
    if persona_text:
        system_parts.append(persona_text)
    if checkpoint_text:
        system_parts.append(checkpoint_text)
    if knowledge_block:
        system_parts.append(knowledge_block)
    system = "\n\n".join(system_parts)

    # Recalculate used with knowledge included
    used = 0
    if system:
        used += count_text(system) + 4
    used += new_msg_tokens
    used += 3

    budget_for_buffer = max_input - used

    # --- 5. Conversation buffer — fill newest-first within remaining budget ---
    buffer_size = cfg.conversation.buffer_messages
    recent = get_recent_messages(user_id, limit=buffer_size)

    candidates = [
        msg for msg in recent
        if msg["role"] in ("user", "assistant")
    ]

    # The new message was already saved to DB before build_context() is called,
    # so it may appear as the last item in recent. Drop it to avoid duplication
    # — it gets appended explicitly as step 6 below.
    if candidates and candidates[-1]["role"] == "user" and candidates[-1]["content"] == new_message:
        candidates = candidates[:-1]

    messages: list[dict] = []
    buffer_tokens = 0

    for msg in reversed(candidates):
        msg_tokens = count_text(msg["content"]) + 4
        if buffer_tokens + msg_tokens > budget_for_buffer:
            break
        messages.append({"role": msg["role"], "content": msg["content"]})
        buffer_tokens += msg_tokens

    messages.reverse()

    # --- 6. New user message — always last ---
    if image_data and image_media_type:
        new_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image_media_type,
                    "data": image_data,
                },
            },
            {"type": "text", "text": new_message},
        ]
        messages.append({"role": "user", "content": new_content})
    else:
        messages.append({"role": "user", "content": new_message})

    total_tokens = used + buffer_tokens
    logger.info(
        "Context assembled: system=%d (knowledge=%d, %d entries), buffer=%d msgs (%d tok), new_msg=%d, total=%d / %d budget",
        count_text(system) if system else 0,
        knowledge_tokens,
        len(entries) if knowledge_block else 0,
        len(messages) - 1,
        buffer_tokens,
        new_msg_tokens,
        total_tokens,
        max_input,
    )

    return system, messages


def _fit_knowledge_to_budget(entries: list[dict], max_tokens: int) -> str:
    """Build a knowledge block fitting as many entries as possible within budget."""
    from src.knowledge import _format_tag

    header = "[From previous conversations, you know the following about this user:]"
    lines = [header]
    tokens_used = count_text(header)

    for entry in entries:
        tag = _format_tag(entry)
        content = entry.get("content", "")
        line = f"- {tag} {content}"

        line_tokens = count_text(line)
        if tokens_used + line_tokens > max_tokens:
            break
        lines.append(line)
        tokens_used += line_tokens

    if len(lines) <= 1:
        return ""  # Only the header, no entries fit
    return "\n".join(lines)
