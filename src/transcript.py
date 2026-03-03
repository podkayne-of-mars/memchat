"""Transcript storage — saves full session transcripts as gzipped JSONL."""

import gzip
import json
import logging
from pathlib import Path

from src.database import get_session_messages

logger = logging.getLogger(__name__)

TRANSCRIPT_DIR = Path("data/transcripts")


def save_transcript(session_id: str) -> str | None:
    """Save a session's user/assistant messages as gzipped JSONL.

    Returns the filename (basename only) on success, None on failure.
    Logs errors but never raises — transcript failure shouldn't block curation.
    """
    try:
        messages = get_session_messages(session_id)
        conversation = [m for m in messages if m["role"] in ("user", "assistant")]
        if not conversation:
            logger.warning("Transcript: no user/assistant messages for session %s", session_id)
            return None

        # Build filename from first message timestamp
        first_ts = conversation[0]["created_at"]  # e.g. "2026-03-02 09:15:42"
        ts_part = first_ts.replace(" ", "_").replace(":", "-")
        # Trim to minute precision (YYYY-MM-DD_HH-MM)
        ts_part = ts_part[:16]
        filename = f"session_{ts_part}.txt.gz"

        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        filepath = TRANSCRIPT_DIR / filename

        # Write gzipped JSONL
        with gzip.open(filepath, "wt", encoding="utf-8") as f:
            for idx, msg in enumerate(conversation):
                line = json.dumps({
                    "index": idx,
                    "role": msg["role"],
                    "content": msg["content"],
                })
                f.write(line + "\n")

        logger.info("Transcript saved: %s (%d messages)", filename, len(conversation))
        return filename

    except Exception:
        logger.exception("Transcript save failed for session %s", session_id)
        return None
