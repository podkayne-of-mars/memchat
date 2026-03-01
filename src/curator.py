"""Curator — extracts knowledge and checkpoints from conversation sessions.

Called during handover: reads the session's messages, sends them to a fast/cheap
model (Haiku by default) with a structured extraction prompt, parses the JSON
response, and writes knowledge entries + a new checkpoint to the database.
"""

import json
import logging
from datetime import date

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

Analyse the conversation and extract:

1. **Facts**: Concrete factual claims stated by the user about themselves, \
their work, their environment, or the world. Examples: "I use Python 3.12", \
"I live in Melbourne", "My dog's name is Rex". NOT pleasantries or \
conversational filler.

2. **Opinions**: Strongly held views, preferences, or tastes expressed by \
the user. Examples: "I hate ORMs", "I prefer tabs over spaces", \
"Heinlein is overrated after 1970". Must be a genuine preference, not \
a passing remark.

3. **Decisions**: Choices made during the conversation that should be \
remembered. Examples: "We decided to use SQLite instead of PostgreSQL", \
"Going with FastAPI not Flask". Include the reasoning if given.

4. **Corrections**: Anything that updates or contradicts a previous \
understanding. Examples: "Actually I switched from VS Code to Neovim", \
"The deadline moved to April". If you can identify what this corrects, note it.

5. **Failed approaches**: Ideas, suggestions, or approaches that were \
proposed and explicitly rejected, abandoned, or found not to work — \
AND the reason why. This is critical. Future conversations must not \
re-suggest things that already failed. Examples: "Tried using Redis for \
caching but it was overkill for the data volume", "Rejected microservices \
architecture because it's just a personal project".

For each extracted entry, assess confidence:
- **high**: Explicitly and clearly stated, no ambiguity
- **medium**: Reasonably implied or partially stated
- **low**: Inferred or uncertain, might be misinterpreting

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
      "type": "fact|opinion|decision|correction|failed_approach",
      "topic": "short topic label (2-5 words)",
      "content": "the actual knowledge entry — be specific and self-contained",
      "confidence": "high|medium|low"
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
    today = str(date.today())

    for entry in knowledge_entries:
        entry_type = entry.get("type", "fact")
        topic = entry.get("topic", "general")
        content = entry.get("content", "")
        confidence = entry.get("confidence", "medium")

        # Validate
        if entry_type not in ("fact", "opinion", "decision", "correction", "failed_approach"):
            logger.warning("Curator: skipping entry with invalid type '%s'", entry_type)
            continue
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        if not content.strip():
            continue

        save_knowledge(
            user_id=user_id,
            entry_type=entry_type,
            topic=topic,
            content=content,
            confidence=confidence,
            source_session_id=session_id,
        )
        saved_count += 1
        logger.info("  Curator extracted [%s] %s: %s", entry_type, topic, content[:80])

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
