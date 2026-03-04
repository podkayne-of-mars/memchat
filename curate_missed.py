"""One-shot script: re-curate the 3 sessions that were missed due to the
background task GC bug. Run from the same shell where ANTHROPIC_API_KEY is set:

    py curate_missed.py
"""

import asyncio
from src.database import init_db
from src.vector_store import init_vector_store
from src.curator import curate_session

SESSIONS = [
    (5, "4c44e712-c1c4-4af8-8d5b-72270c911cf1", "session_2026-03-03_08-49.txt.gz"),
    (5, "041a1d0a-eac6-478c-9474-61bbab527611", "session_2026-03-03_10-38.txt.gz"),
    (5, "477edb16-7891-4092-b524-aa56ce17e028", "session_2026-03-03_10-45.txt.gz"),
]


async def main():
    init_db()
    init_vector_store()
    for user_id, session_id, transcript in SESSIONS:
        print(f"Curating {session_id[:12]}... ({transcript})")
        result = await curate_session(user_id, session_id, transcript_file=transcript)
        count = result.get("knowledge_count", 0)
        summary = (result.get("checkpoint_summary") or "(none)")[:80]
        print(f"  -> {count} entries, checkpoint: {summary}")
        if result.get("error"):
            print(f"  ERROR: {result['error']}")
        print()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
