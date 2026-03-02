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
    """Format knowledge entries into a tagged block for the system prompt.

    Each entry is formatted as: - [TYPE:category:DATE:SALIENCE] content
    Returns empty string if no entries.
    """
    if not entries:
        return ""

    lines = ["[From previous conversations, you know the following about this user:]"]

    for entry in entries:
        tag = _format_tag(entry)
        content = entry.get("content", "")
        lines.append(f"- {tag} {content}")

    return "\n".join(lines)


def _format_date(raw: str | None) -> str:
    """Extract YYYY-MM-DD from a timestamp or date string."""
    if not raw:
        return ""
    return raw[:10]


def _format_tag(entry: dict) -> str:
    """Build a structured tag: [TYPE:category:DATE:SALIENCE]."""
    entry_type = entry.get("type", "fact").upper()
    category = entry.get("topic", "")
    salience = (entry.get("salience") or "low").upper()
    # Prefer event_date; fall back to created_at
    date_str = _format_date(entry.get("event_date") or entry.get("created_at"))
    return f"[{entry_type}:{category}:{date_str}:{salience}]"


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
