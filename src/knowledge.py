"""Knowledge retrieval — search and format knowledge entries for context injection."""

import logging
import re

from src.config import get_config
from src.database import get_all_active_knowledge, search_knowledge as fts_search

logger = logging.getLogger(__name__)


def retrieve_knowledge(user_id: int, query: str) -> list[dict]:
    """Search for relevant active knowledge entries using FTS5.

    Falls back to returning all active entries (most recent first) if the
    FTS query fails — FTS5 MATCH syntax can choke on special characters.

    Returns entries ordered by relevance, limited to max_knowledge_entries
    from config.
    """
    cfg = get_config()
    limit = cfg.conversation.max_knowledge_entries

    # Sanitise the query for FTS5 — strip characters that break MATCH syntax
    clean = _sanitise_fts_query(query)

    if not clean:
        # Nothing searchable after sanitisation — return recent entries
        entries = get_all_active_knowledge(user_id)
        return entries[:limit]

    try:
        entries = fts_search(user_id, clean, limit=limit)
    except Exception as exc:
        # FTS5 can fail on weird query syntax — fall back gracefully
        logger.warning("FTS5 search failed for query '%s': %s. Falling back to all entries.", clean, exc)
        entries = get_all_active_knowledge(user_id)
        entries = entries[:limit]

    return entries


def format_knowledge_block(entries: list[dict]) -> str:
    """Format knowledge entries into a natural text block for the system prompt.

    Returns a string suitable for injection after the checkpoint in the system
    prompt. Returns empty string if no entries.
    """
    if not entries:
        return ""

    lines = ["[From previous conversations, you know the following about this user:]"]

    for entry in entries:
        entry_type = entry.get("type", "fact")
        topic = entry.get("topic", "")
        content = entry.get("content", "")
        confidence = entry.get("confidence", "medium")

        prefix = _type_prefix(entry_type)
        conf_note = f" (uncertain)" if confidence == "low" else ""

        lines.append(f"- {prefix}{topic}: {content}{conf_note}")

    return "\n".join(lines)


def _type_prefix(entry_type: str) -> str:
    """Return a short readable prefix for the knowledge type."""
    return {
        "fact": "",
        "opinion": "Preference — ",
        "decision": "Decision — ",
        "correction": "Correction — ",
        "failed_approach": "Rejected approach — ",
    }.get(entry_type, "")


def _sanitise_fts_query(query: str) -> str:
    """Clean a user message into something safe for FTS5 MATCH.

    FTS5 MATCH chokes on bare punctuation, unmatched quotes, and operators.
    We extract just the words and OR them together for broad matching.
    """
    # Extract word tokens only
    words = re.findall(r"[a-zA-Z0-9]+", query)
    # Drop very short words that add noise
    words = [w for w in words if len(w) > 2]
    if not words:
        return ""
    # OR them for broad matching (any word matches)
    return " OR ".join(words)
