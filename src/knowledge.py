"""Knowledge retrieval — search and format knowledge entries for context injection."""

import logging
import re

from src.config import get_config
from src.database import get_all_active_knowledge, get_knowledge_by_ids
from src.vector_store import search_knowledge as vector_search

logger = logging.getLogger(__name__)


def retrieve_knowledge(user_id: int, query: str) -> list[dict]:
    """Search for relevant knowledge entries using ChromaDB vector similarity.

    Falls back to returning all active entries if the vector store returns
    nothing (e.g. empty collection, no matches above threshold).

    Returns entries ordered by similarity, limited to max_knowledge_entries.
    """
    cfg = get_config()
    limit = cfg.conversation.max_knowledge_entries

    try:
        ids = vector_search(user_id, query, n_results=limit)
    except Exception as exc:
        logger.warning("Vector search failed: %s. Falling back to all entries.", exc)
        ids = []

    if ids:
        entries = get_knowledge_by_ids(ids)
    else:
        # Fallback: return all active entries if vector search returned nothing
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

    Legacy — no longer called by retrieve_knowledge() (ChromaDB vector search
    replaced FTS5 for retrieval). Kept for potential direct FTS5 use.
    """
    # Extract word tokens only
    words = re.findall(r"[a-zA-Z0-9]+", query)
    # Drop very short words that add noise
    words = [w for w in words if len(w) > 2]
    if not words:
        return ""
    # OR them for broad matching (any word matches)
    return " OR ".join(words)
