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
category, content, and retention dimensions.

## Types

- **fact**: Concrete factual claims about the user, their life, environment, \
or world.
- **preference**: Strongly held views, tastes, or preferences.
- **decision**: Choices made during the conversation that should stick.
- **correction**: Anything that updates or contradicts a previous understanding.
- **rejected**: Ideas/approaches proposed and explicitly rejected — AND the reason.
- **event**: Something that happened — life event, milestone, occurrence.
- **project**: Information about an ongoing project — status, goals, architecture, \
milestones reached, implementation details, what changed this session.
- **action**: Changes made during this session — file edits, implementations, \
configuration changes. Include what was changed and why.

## Retention

Each entry has two retention dimensions:

- **continuity**: HIGH or LOW — is this needed to resume current work? \
HIGH for: implementation details, file changes, active bugs, where we left off, \
architectural decisions for active projects. \
Decays once the project/task is resolved.

- **durable**: HIGH or LOW — does this matter about the user long-term? \
HIGH for: life events, personal facts, preferences, corrections, things they'd \
expect you to remember in a year. \
Doesn't decay.

## Category

A short consistent label (2-4 words) grouping related entries. Use the same \
category for entries about the same thread — don't vary the name. \
Examples: "coding style", "memchat architecture", "career", "reading tastes".

## Event date

YYYY-MM-DD when it happened (not when discussed). Null if unknown or \
not applicable.

## What to capture

Be greedy. Extract more rather than less. Look for:
- Milestones: features completed, bugs fixed, things that now work
- Implementation details: what files changed, what was added, how it works
- Status changes: "X is now done" vs "we discussed X"
- Breadcrumbs: enough detail that a future session can pick up where this left off
- Life events: anything personal, even if mentioned in passing
- Preferences and corrections: especially if they'd be annoyed if you forgot

Ask yourself: if this session ended abruptly, what would the next instance need \
to know to continue? And separately: what would matter about this user in a year?

## Checkpoint

ALSO produce a checkpoint: a brief narrative summary (2-4 sentences) of \
where the conversation currently stands. Focus on active work, open questions, \
and next steps. Also list the active discussion topics as short labels.

If there is nothing worth extracting (e.g. the conversation was just \
greetings or small talk), return empty arrays — do NOT invent entries.

## Source references

Messages in the conversation are numbered with indices [0], [1], etc. \
When an entry summarises reasoning, detailed discussion, or theory where \
the full original context would be valuable, add a source_ref pointing to \
the relevant message range. Self-contained facts and preferences don't need it.

Return this exact JSON structure:
{
  "knowledge": [
    {
      "type": "fact|preference|decision|correction|rejected|event|project|action",
      "category": "short consistent label",
      "content": "the actual knowledge entry — be specific and self-contained",
      "continuity": "high|low",
      "durable": "high|low",
      "event_date": "YYYY-MM-DD or null",
      "source_ref": {"from_msg": N, "to_msg": M} or null
    }
  ],
  "checkpoint": {
    "summary": "Natural narrative summary of where the conversation stands...",
    "active_topics": ["topic1", "topic2"]
  }
}
"""


def _build_curator_messages(session_messages: list[dict]) -> list[dict]:
    """Format session messages into the conversation block for the curator prompt.

    Each user/assistant message is numbered with a sequential index so the
    curator can reference specific message ranges in source_ref fields.
    """
    lines = []
    idx = 0
    for msg in session_messages:
        role = msg["role"]
        if role == "system":
            continue
        speaker = "User" if role == "user" else "Assistant"
        lines.append(f"[{idx}] {speaker}: {msg['content']}")
        idx += 1

    conversation_text = "\n\n".join(lines)
    return [{"role": "user", "content": f"Here is the conversation to analyse:\n\n{conversation_text}"}]


async def curate_session(user_id: int, session_id: str, transcript_file: str | None = None) -> dict:
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

    valid_types = ("fact", "preference", "decision", "correction", "rejected", "event", "project", "action")

    for entry in knowledge_entries:
        entry_type = entry.get("type", "fact")
        category = entry.get("category", "general")
        content = entry.get("content", "")
        continuity = entry.get("continuity", "low")
        durable = entry.get("durable", "low")
        event_date = entry.get("event_date")

        # Validate
        if entry_type not in valid_types:
            logger.warning("Curator: skipping entry with invalid type '%s'", entry_type)
            continue
        if continuity not in ("high", "low"):
            continuity = "low"
        if durable not in ("high", "low"):
            durable = "low"
        if not content.strip():
            continue
        # Normalise event_date: keep only non-empty strings
        if not event_date or not isinstance(event_date, str):
            event_date = None

        # Build source_ref if the curator provided message indices and we have a transcript
        source_ref = None
        if transcript_file and entry.get("source_ref"):
            ref = entry["source_ref"]
            if isinstance(ref, dict) and "from_msg" in ref and "to_msg" in ref:
                source_ref = json.dumps({
                    "file": transcript_file,
                    "from_msg": ref["from_msg"],
                    "to_msg": ref["to_msg"],
                })

        save_knowledge(
            user_id=user_id,
            entry_type=entry_type,
            topic=category,
            content=content,
            continuity=continuity,
            durable=durable,
            event_date=event_date,
            source_session_id=session_id,
            source_ref=source_ref,
        )
        saved_count += 1
        logger.info("  Curator extracted [%s:C=%s:D=%s] %s", entry_type, continuity, durable, content[:80])

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
