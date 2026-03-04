"""Full-loop integration test: conversation → curator → knowledge retrieval → new session.

Exercises the complete memory pipeline:
1. Inserts a test conversation with extractable knowledge
2. Runs the curator to extract knowledge + checkpoint
3. Starts a "new session" (simulating post-handover)
4. Builds context for a follow-up message — should include retrieved knowledge
5. Sends the message to Claude — response should reference stored knowledge

Run from project root:
    py tests/test_full_loop.py

Requires MEMCHAT_API_KEY in the environment.
"""

import asyncio
import json
import sys
import os

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
from src.anthropic_client import complete_message

TEST_DB = "./data/test_full_loop.db"
SESSION_1 = "session-original-conversation"
SESSION_2 = "session-after-handover"
USER_ID = 1

CONVERSATION = [
    ("user", "I've been thinking about science fiction lately. I really prefer Heinlein over Asimov — Heinlein's characters feel more alive, while Asimov's are basically mouthpieces for ideas."),
    ("assistant", "That's a common take and I think there's real merit to it. Heinlein was much more interested in how people *live* in the future rather than just the mechanics of it. Though I'd argue Asimov's strength was never meant to be characterisation — he was building logical puzzles. Which Heinlein works are your favourites?"),
    ("user", "The Moon is a Harsh Mistress is probably my number one. I've actually been to Tokyo twice and there's a bookshop in Jimbocho that has an incredible English-language sci-fi section — that's where I found a first edition of Podkayne of Mars."),
    ("assistant", "Jimbocho is legendary for bookshops. Finding a first edition Podkayne there is quite a score. The Moon is a Harsh Mistress is an excellent choice though. The political philosophy in it is fascinating. What draws you to it most — the revolution narrative or the AI character?"),
    ("user", "Mostly the revolution and the libertarian political philosophy. Mike the AI is great but it's really the 'rational anarchism' stuff that stuck with me. Anyway, switching topics — for this chat app we're building, let's use SQLite not Postgres. We don't need a client-server database for what's essentially a single-user local app."),
    ("assistant", "SQLite is absolutely the right call here. You get ACID transactions, full-text search with FTS5, zero deployment complexity, and the whole database is a single file you can back up by copying. Postgres would be massive overkill — you'd need to run a server process, manage connections, handle configuration. For a local-first app like this, SQLite is the obvious choice. Did you consider any caching layer?"),
    ("user", "We considered Redis for caching frequently accessed knowledge entries, but honestly it's overkill. The SQLite database will be small enough that queries will be fast without a cache layer. Adding Redis means another service to run, another thing to break. Keep it simple."),
    ("assistant", "Completely agree. Redis would add operational complexity for negligible benefit at this scale. SQLite with WAL mode is already very fast for reads, and your knowledge store is unlikely to exceed what fits comfortably in the OS page cache anyway. YAGNI applies perfectly here."),
    ("user", "Oh wait, I said I'd been to Tokyo twice earlier — actually it was three times. The first trip was in 2015 and I'd forgotten about it. The Jimbocho bookshop visit was on the third trip."),
    ("assistant", "Good catch — three trips to Tokyo then, with the Jimbocho bookshop discovery on the most recent one. That 2015 trip must have been early enough that you might not have been hunting for English sci-fi specifically."),
    ("user", "Yeah, the collecting started around 2018. Before that I was purely digital. Now I specifically hunt for classic paperbacks when I travel — there's something about the cover art from the 60s and 70s that modern covers just can't match."),
    ("assistant", "The pulp-era and New Wave cover art is genuinely incredible. Artists like Richard Powers, Chris Foss, and Peter Jones created these wild, almost psychedelic visions of the future. The modern trend of minimalist typography-heavy covers feels sterile by comparison. Do you focus on any particular era or publisher for your collection?"),
]


def setup():
    """Create test database with user, persona, session, and conversation."""
    import pathlib
    for suffix in ("", "-wal", "-shm"):
        pathlib.Path(TEST_DB + suffix).unlink(missing_ok=True)

    set_db_path(TEST_DB)
    init_db()

    create_user("allan", "Allan")
    set_persona(USER_ID, (
        "You are engaged in an ongoing conversation with Allan about science fiction, "
        "literature, and related topics. Allan is an experienced reader with particular "
        "interests in Heinlein, Douglas Adams, Terry Pratchett, and classic space opera. "
        "He has strong opinions, appreciates direct disagreement, and communicates with "
        "Australian vernacular mixed with literary references."
    ))

    create_session(SESSION_1, USER_ID)
    for role, content in CONVERSATION:
        save_message(USER_ID, role, content, SESSION_1)

    print(f"Setup: {len(CONVERSATION)} messages in session 1")
    print()


async def phase_1_curate():
    """Run the curator on the original conversation."""
    print("=" * 70)
    print("PHASE 1: CURATOR EXTRACTION")
    print("=" * 70)
    print()

    result = await curate_session(USER_ID, SESSION_1)

    if result.get("error"):
        print(f"CURATOR ERROR: {result['error']}")
        return False

    entries = get_all_active_knowledge(USER_ID)
    print(f"Extracted {len(entries)} knowledge entries:")
    for entry in entries:
        print(f"  [{entry['type']}] {entry['topic']}: {entry['content'][:80]}...")
    print()

    checkpoint = get_active_checkpoint(USER_ID)
    if checkpoint:
        print(f"Checkpoint: {checkpoint['summary'][:120]}...")
    print()
    return True


def phase_2_context():
    """Build context for a new-session message and show knowledge injection."""
    print("=" * 70)
    print("PHASE 2: NEW SESSION CONTEXT ASSEMBLY")
    print("=" * 70)
    print()

    query = "remind me why we picked SQLite and whether we considered any caching"

    # Create a new session (simulating post-handover)
    create_session(SESSION_2, USER_ID)

    system, messages = build_context(USER_ID, query)

    print("SYSTEM PROMPT:")
    print("-" * 70)
    if system:
        # Print in sections so the knowledge block stands out
        sections = system.split("\n\n")
        for i, section in enumerate(sections):
            if i > 0:
                print()
            # Highlight the knowledge block
            if section.startswith("[From previous"):
                print(">>> KNOWLEDGE BLOCK (injected from memory) >>>")
            print(section)
            if section.startswith("[From previous") or (i > 0 and sections[i-1].startswith("[From previous")):
                if not section.startswith("[From previous"):
                    pass
        # Check if knowledge was found
        if "[From previous" not in system:
            print()
            print("*** WARNING: No knowledge entries found in system prompt! ***")
    print()

    print(f"MESSAGES ({len(messages)} total):")
    print("-" * 70)
    for i, msg in enumerate(messages):
        content = msg["content"]
        if len(content) > 100:
            content = content[:100] + "..."
        print(f"  [{i+1}] {msg['role'].upper()}: {content}")
    print()

    return system, messages


async def phase_3_response(system, messages):
    """Send the message to Claude and print the response."""
    print("=" * 70)
    print("PHASE 3: CLAUDE'S RESPONSE (should reference stored knowledge)")
    print("=" * 70)
    print()

    cfg = get_config()
    try:
        response = await complete_message(
            messages=messages,
            system=system,
            model=cfg.anthropic.conversation_model,
            max_tokens=1024,
        )
        print(response)
    except Exception as exc:
        print(f"ERROR: {exc}")
    print()


async def main():
    cfg = get_config()
    if not cfg.anthropic.api_key:
        print("ERROR: MEMCHAT_API_KEY not set.")
        sys.exit(1)

    print(f"Curator model: {cfg.anthropic.curator_model}")
    print(f"Chat model: {cfg.anthropic.conversation_model}")
    print()

    setup()

    ok = await phase_1_curate()
    if not ok:
        return

    system, messages = phase_2_context()

    await phase_3_response(system, messages)

    print("=" * 70)
    print(f"Test DB preserved at {TEST_DB}")
    print("Delete with:  rm data/test_full_loop.db*")


if __name__ == "__main__":
    asyncio.run(main())
