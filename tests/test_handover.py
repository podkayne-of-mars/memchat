"""End-to-end test of the curator and handover pipeline.

Inserts a realistic conversation into the DB, calls the curator,
and prints everything it extracted. Run from the project root:

    py tests/test_handover.py

Requires MEMCHAT_API_KEY in the environment.
"""

import asyncio
import json
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import get_config
from src.database import (
    init_db,
    set_db_path,
    create_user,
    set_persona,
    create_session,
    save_message,
    get_all_active_knowledge,
    get_active_checkpoint,
)
from src.curator import curate_session
from src.assembler import build_context

TEST_DB = "./data/test_handover.db"
SESSION_ID = "test-handover-session-001"
USER_ID = 1

# --- Simulated conversation ---
# Contains: preference, factual claim, decision, rejected idea, correction

CONVERSATION = [
    ("user", "I've been thinking about science fiction lately. I really prefer Heinlein over Asimov — Heinlein's characters feel more alive, while Asimov's are basically mouthpieces for ideas."),

    ("assistant", "That's a common take and I think there's real merit to it. Heinlein was much more interested in how people *live* in the future rather than just the mechanics of it. Though I'd argue Asimov's strength was never meant to be characterisation — he was building logical puzzles. Which Heinlein works are your favourites?"),

    ("user", "The Moon is a Harsh Mistress is probably my number one. I've actually been to Tokyo twice and there's a bookshop in Jimbocho that has an incredible English-language sci-fi section — that's where I found a first edition of Podkayne of Mars."),

    ("assistant", "Jimbocho is legendary for bookshops. Finding a first edition Podkayne there is quite a score — that's not one of Heinlein's most celebrated works so first editions aren't as hunted as, say, Stranger in a Strange Land. The Moon is a Harsh Mistress is an excellent choice though. The political philosophy in it is fascinating. What draws you to it most — the revolution narrative or the AI character?"),

    ("user", "Mostly the revolution and the libertarian political philosophy. Mike the AI is great but it's really the 'rational anarchism' stuff that stuck with me. Anyway, switching topics — for this chat app we're building, let's use SQLite not Postgres. We don't need a client-server database for what's essentially a single-user local app."),

    ("assistant", "SQLite is absolutely the right call here. You get ACID transactions, full-text search with FTS5, zero deployment complexity, and the whole database is a single file you can back up by copying. Postgres would be massive overkill — you'd need to run a server process, manage connections, handle configuration. For a local-first app like this, SQLite is the obvious choice. Did you consider any caching layer?"),

    ("user", "We considered Redis for caching frequently accessed knowledge entries, but honestly it's overkill. The SQLite database will be small enough that queries will be fast without a cache layer. Adding Redis means another service to run, another thing to break. Keep it simple."),

    ("assistant", "Completely agree. Redis would add operational complexity for negligible benefit at this scale. SQLite with WAL mode is already very fast for reads, and your knowledge store is unlikely to exceed what fits comfortably in the OS page cache anyway. YAGNI applies perfectly here."),

    ("user", "Oh wait, I said I'd been to Tokyo twice earlier — actually it was three times. The first trip was in 2015 and I'd forgotten about it. The Jimbocho bookshop visit was on the third trip."),

    ("assistant", "Good catch — three trips to Tokyo then, with the Jimbocho bookshop discovery on the most recent one. That 2015 trip must have been early enough that you might not have been hunting for English sci-fi specifically. Did your interest in collecting physical copies develop later?"),

    ("user", "Yeah, the collecting started around 2018. Before that I was purely digital. Now I specifically hunt for classic paperbacks when I travel — there's something about the cover art from the 60s and 70s that modern covers just can't match."),

    ("assistant", "The pulp-era and New Wave cover art is genuinely incredible. Artists like Richard Powers, Chris Foss, and Peter Jones created these wild, almost psychedelic visions of the future. The modern trend of minimalist typography-heavy covers feels sterile by comparison. Do you focus on any particular era or publisher for your collection?"),
]


def setup():
    """Create test database with user, persona, session, and messages."""
    # Clean slate
    import pathlib
    pathlib.Path(TEST_DB).unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        pathlib.Path(TEST_DB + suffix).unlink(missing_ok=True)

    set_db_path(TEST_DB)
    init_db()

    # Create user with persona
    create_user("allan", "Allan")
    set_persona(USER_ID, (
        "You are engaged in an ongoing conversation with Allan about science fiction, "
        "literature, and related topics. Allan is an experienced reader with particular "
        "interests in Heinlein, Douglas Adams, Terry Pratchett, and classic space opera. "
        "He has strong opinions, appreciates direct disagreement, and communicates with "
        "Australian vernacular mixed with literary references."
    ))

    # Create session and insert messages
    create_session(SESSION_ID, USER_ID)
    for role, content in CONVERSATION:
        save_message(USER_ID, role, content, SESSION_ID)

    print(f"Setup complete: {len(CONVERSATION)} messages in session {SESSION_ID}")
    print()


async def run_curator():
    """Call the curator and print what it extracted."""
    print("=" * 70)
    print("CALLING CURATOR (Opus)...")
    print("=" * 70)
    print()

    result = await curate_session(USER_ID, SESSION_ID)

    if result.get("error"):
        print(f"CURATOR ERROR: {result['error']}")
        return False

    # Print knowledge entries
    entries = get_all_active_knowledge(USER_ID)
    print(f"KNOWLEDGE ENTRIES EXTRACTED: {len(entries)}")
    print("-" * 70)
    for entry in entries:
        print(f"  [{entry['type'].upper()}] (confidence: {entry['confidence']})")
        print(f"  Topic: {entry['topic']}")
        print(f"  Content: {entry['content']}")
        print()

    # Print checkpoint
    checkpoint = get_active_checkpoint(USER_ID)
    print("-" * 70)
    print("CHECKPOINT:")
    if checkpoint:
        print(f"  Summary: {checkpoint['summary']}")
        print(f"  Active topics: {checkpoint['active_topics']}")
    else:
        print("  (none)")
    print()

    return True


def test_next_context():
    """Show what build_context() would produce for a follow-up message."""
    print("=" * 70)
    print("NEXT CONTEXT: build_context(user_id=1, 'remind me why we picked SQLite')")
    print("=" * 70)
    print()

    system, messages = build_context(USER_ID, "remind me why we picked SQLite")

    print("SYSTEM PROMPT:")
    print("-" * 70)
    if system:
        print(system)
    else:
        print("(none)")
    print()

    print(f"MESSAGES ({len(messages)} total):")
    print("-" * 70)
    for i, msg in enumerate(messages):
        label = msg["role"].upper()
        content = msg["content"]
        if len(content) > 120:
            content = content[:120] + "..."
        print(f"  [{i+1}] {label}: {content}")
    print()


async def main():
    cfg = get_config()
    if not cfg.anthropic.api_key:
        print("ERROR: MEMCHAT_API_KEY not set in environment.")
        print("Set it and re-run:  export MEMCHAT_API_KEY='sk-ant-...'")
        sys.exit(1)

    print(f"Using curator model: {cfg.anthropic.curator_model}")
    print()

    setup()

    ok = await run_curator()
    if ok:
        test_next_context()

    # Cleanup
    print("=" * 70)
    print(f"Test DB preserved at {TEST_DB} for inspection.")
    print("Delete with:  rm data/test_handover.db*")


if __name__ == "__main__":
    asyncio.run(main())
