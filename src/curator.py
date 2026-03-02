"""Curator — extracts knowledge and checkpoints from conversation sessions.

Called during handover: reads the session's messages, sends them to a fast/cheap
model (Opus by default) with a structured extraction prompt, parses the JSON
response, and writes knowledge entries + a new checkpoint to the database.
"""

import json
import logging

from src.anthropic_client import AnthropicError, complete_message
from src.config import get_config
from src.database import (
    get_active_checkpoint,
    get_session_messages,
    save_checkpoint,
    save_knowledge,
)

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """\
You are a knowledge extraction system. Your job is to read a conversation \
between a user and an AI assistant and extract information worth remembering \
for future conversations.

IMPORTANT: Return ONLY valid JSON. No preamble, no explanation, no markdown \
fences. Your entire response must be a single JSON object.

Analyse the conversation and extract knowledge entries. Each entry has a type, \
category, content, salience, and optional event_date.

## Types

- **fact**: Concrete factual claims about the user, their life, environment, \
or world. "I use Python 3.12", "I live in Melbourne", "My dog's name is Rex". \
NOT pleasantries or conversational filler.
- **preference**: Strongly held views, tastes, or preferences. "I hate ORMs", \
"I prefer tabs over spaces", "Heinlein is overrated after 1970". Must be a \
genuine preference, not a passing remark.
- **decision**: Choices made during the conversation that should stick. \
"We decided to use SQLite instead of PostgreSQL", "Going with FastAPI not Flask". \
Include the reasoning if given.
- **correction**: Anything that updates or contradicts a previous understanding. \
"Actually I switched from VS Code to Neovim", "The deadline moved to April".
- **rejected**: Ideas, suggestions, or approaches that were proposed and \
explicitly rejected, abandoned, or found not to work — AND the reason why. \
This is critical. Future conversations must not re-suggest things that already \
failed. "Tried Redis for caching but overkill for the data volume".
- **event**: Something that happened — a life event, milestone, or occurrence. \
"Got a new job at Acme Corp", "Moved to a new apartment". Set event_date to \
when it happened (not when discussed), if known.
- **project**: Information about an ongoing project, its status, goals, or \
architecture. "Memchat uses ChromaDB for vector search", "The API is FastAPI \
with SSE streaming".

## Salience

Salience determines how aggressively the AI surfaces this knowledge in future \
conversations. Assign HIGH or LOW:

- **HIGH**: The user would be frustrated or confused if the AI forgot this. \
Preferences, decisions, corrections, rejected approaches, and important personal \
facts. Things the user explicitly stated or emphasised. Things that change how \
the AI should behave.
- **LOW**: Useful background context but not critical. General facts, project \
details that are easy to re-state, events mentioned in passing.

When in doubt, prefer HIGH — it's better to over-surface than to forget.

## Category

A short consistent label (2-4 words) grouping related entries. Use the same \
category for entries about the same thread — don't vary the name. \
Examples: "coding style", "memchat architecture", "career", "reading tastes".

## Event date

For events: the date (YYYY-MM-DD) when the event happened, not when it was \
discussed. For decisions: the date the decision was made, if clear. \
Omit (null) if unknown or not applicable.

## Checkpoint

ALSO produce a checkpoint: a brief narrative summary (2-4 sentences) of \
where the conversation currently stands. This will be injected into the \
system prompt of future conversations, so it should read naturally — \
NOT a bullet list, but a flowing summary. Include what was being discussed, \
any open questions, and what the user might want to continue with next. \
Also list the active discussion topics as short labels.

If there is nothing worth extracting (e.g. the conversation was just \
greetings or small talk), return empty arrays — do NOT invent entries.

Return this exact JSON structure:
{
  "knowledge": [
    {
      "type": "fact|preference|decision|correction|rejected|event|project",
      "category": "short consistent label",
      "content": "the actual knowledge entry — be specific and self-contained",
      "salience": "high|low",
      "event_date": "YYYY-MM-DD or null"
    }
  ],
  "checkpoint": {
    "summary": "Natural narrative summary of where the conversation stands...",
    "active_topics": ["topic1", "topic2"]
  }
}
"""


def _build_curator_messages(session_messages: list[dict]) -> list[dict]:
    """Format session messages into the conversation block for the curator prompt."""
    lines = []
    for msg in session_messages:
        role = msg["role"]
        if role == "system":
            continue
        speaker = "User" if role == "user" else "Assistant"
        lines.append(f"{speaker}: {msg['content']}")

    conversation_text = "\n\n".join(lines)
    return [{"role": "user", "content": f"Here is the conversation to analyse:\n\n{conversation_text}"}]


async def curate_session(user_id: int, session_id: str) -> dict:
    """Extract knowledge and checkpoint from a session's messages.

    Returns a summary dict: {"knowledge_count": N, "checkpoint_summary": "..."}.
    On failure, logs the error and returns {"knowledge_count": 0, "error": "..."}.
    """
    cfg = get_config()

    # Load session messages
    messages = get_session_messages(session_id)
    if not messages:
        logger.warning("Curator: no messages found for session %s", session_id)
        return {"knowledge_count": 0, "checkpoint_summary": "", "error": "No messages in session"}

    # Filter to just user/assistant
    conversation = [m for m in messages if m["role"] in ("user", "assistant")]
    if not conversation:
        logger.warning("Curator: no user/assistant messages in session %s", session_id)
        return {"knowledge_count": 0, "checkpoint_summary": "", "error": "No conversation messages"}

    logger.info(
        "Curator: processing session %s (%d messages) for user %d",
        session_id, len(conversation), user_id,
    )

    # Include existing checkpoint as context so the curator can build on it
    existing_checkpoint = get_active_checkpoint(user_id)
    system = EXTRACTION_PROMPT
    if existing_checkpoint:
        system += (
            f"\n\nFor context, here is the previous checkpoint summary:\n"
            f"{existing_checkpoint['summary']}"
        )

    curator_messages = _build_curator_messages(conversation)

    # Call the curator model
    try:
        raw_response = await complete_message(
            messages=curator_messages,
            system=system,
            model=cfg.anthropic.curator_model,
            max_tokens=4096,
        )
    except AnthropicError as exc:
        logger.error("Curator API call failed for session %s: %s", session_id, exc)
        return {"knowledge_count": 0, "checkpoint_summary": "", "error": str(exc)}

    # Parse the JSON response
    try:
        data = _parse_curator_response(raw_response)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error(
            "Curator: failed to parse response for session %s: %s\nRaw response: %s",
            session_id, exc, raw_response[:500],
        )
        return {"knowledge_count": 0, "checkpoint_summary": "", "error": f"Parse error: {exc}"}

    # Write knowledge entries
    knowledge_entries = data.get("knowledge", [])
    saved_count = 0

    valid_types = ("fact", "preference", "decision", "correction", "rejected", "event", "project")

    for entry in knowledge_entries:
        entry_type = entry.get("type", "fact")
        category = entry.get("category", "general")
        content = entry.get("content", "")
        salience = entry.get("salience", "low")
        event_date = entry.get("event_date")

        # Validate
        if entry_type not in valid_types:
            logger.warning("Curator: skipping entry with invalid type '%s'", entry_type)
            continue
        if salience not in ("high", "low"):
            salience = "low"
        if not content.strip():
            continue
        # Normalise event_date: keep only non-empty strings
        if not event_date or not isinstance(event_date, str):
            event_date = None

        save_knowledge(
            user_id=user_id,
            entry_type=entry_type,
            topic=category,
            content=content,
            salience=salience,
            event_date=event_date,
            source_session_id=session_id,
        )
        saved_count += 1
        logger.info("  Curator extracted [%s:%s] %s", entry_type, salience, content[:80])

    # Write checkpoint
    checkpoint = data.get("checkpoint", {})
    checkpoint_summary = checkpoint.get("summary", "")
    active_topics = checkpoint.get("active_topics", [])

    if checkpoint_summary:
        save_checkpoint(
            user_id=user_id,
            summary=checkpoint_summary,
            active_topics=json.dumps(active_topics),
        )
        logger.info("  Curator checkpoint: %s", checkpoint_summary[:120])
    else:
        logger.warning("Curator: no checkpoint summary in response for session %s", session_id)

    logger.info(
        "Curator done: session %s — %d knowledge entries, checkpoint: %s",
        session_id, saved_count, "yes" if checkpoint_summary else "no",
    )

    return {
        "knowledge_count": saved_count,
        "checkpoint_summary": checkpoint_summary,
    }


def _parse_curator_response(raw: str) -> dict:
    """Parse the curator's JSON response, handling common LLM output quirks."""
    text = raw.strip()

    # Strip markdown fences if the model ignored our instructions
    if text.startswith("```"):
        # Remove opening fence (with optional language tag)
        first_newline = text.index("\n")
        text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    data = json.loads(text)

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")
    if "knowledge" not in data and "checkpoint" not in data:
        raise ValueError("Response missing both 'knowledge' and 'checkpoint' keys")

    return data
