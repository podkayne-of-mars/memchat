# Project: Memchat
## A Stateless Chat Client That Never Forgets

### Overview

A local desktop chat application that presents a continuous, unbroken conversation with Claude. The user just chats. Behind the scenes, the client manages API sessions, extracts knowledge, and rebuilds context invisibly. The conversation never ends, never degrades, and never forgets.

### Design Philosophy

- **Magic, not maintenance.** Zero manual curation. No file management. No "remember to save." The user opens the app, types, and picks up where they left off.
- **Local first.** All data stored locally. No cloud services beyond the Anthropic API for Claude calls. No accounts, no SaaS, no telemetry.
- **Portable.** Designed to run on the dev machine now, move to a Raspberry Pi, mini PC, or NAS later. Docker-ready architecture but not Docker-dependent.
- **Multi-user from day one.** Separate personas, knowledge stores, and conversation histories per user. Designed for household use on a local network.

---

### Architecture

```
┌─────────────────────────────────────────────────┐
│                  Browser Tab                     │
│            http://localhost:8080                  │
│                                                   │
│   ┌───────────────────────────────────────────┐   │
│   │          Chat Window (HTML/JS)            │   │
│   │  - Scrolling message history              │   │
│   │  - Input box                              │   │
│   │  - User switcher / login                  │   │
│   └───────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────┘
                       │ HTTP/WebSocket
┌──────────────────────┴──────────────────────────┐
│              FastAPI Backend (Python)             │
│                                                   │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────┐ │
│  │   Context    │  │    Token     │  │ Curator │ │
│  │  Assembler   │  │   Counter    │  │ (Opus)  │ │
│  └──────┬──────┘  └──────┬───────┘  └────┬────┘ │
│         │                │               │       │
│  ┌──────┴────────────────┴───────────────┴────┐  │
│  │            Session Manager                  │  │
│  │  - Tracks context usage per user            │  │
│  │  - Triggers curator at threshold            │  │
│  │  - Manages invisible handover               │  │
│  └─────────────────────┬──────────────────────┘  │
│                        │                          │
│  ┌─────────────────────┴──────────────────────┐  │
│  │              Data Layer                     │  │
│  │  SQLite: conversations, knowledge,          │  │
│  │          checkpoints, users, personas       │  │
│  └────────────────────────────────────────────┘  │
└──────────────────────┬──────────────────────────┘
                       │ HTTPS
                       ▼
              Anthropic API (Claude)
```

---

### Core Components

#### 1. Chat Frontend
- Simple HTML/JS/CSS served by FastAPI
- Scrolling conversation display showing full history from local DB
- Markdown rendering for Claude responses
- Image support: paste, drag-and-drop, or file upload (JPEG/PNG/GIF/WebP, max 10 MB). Preview strip above input, rendered inline in message bubbles. Stored in DB and restored on history load.
- User login/switcher (simple — home network security only)
- No framework bloat. HTMX or vanilla JS. Server-rendered where possible.

#### 2. Context Assembler
Builds the API payload for each message. Assembles from:
1. **System prompt** — user's persona file
2. **Checkpoint** — current "state of the world" summary
3. **Retrieved knowledge** — entries from the knowledge store relevant to the current message (ChromaDB vector search)
4. **Conversation buffer** — last N message pairs from the conversation log (ensures conversational continuity across invisible session boundaries)
5. **User's new message**

The assembler must respect token limits. Priority order if space is tight:
- System prompt (persona) — always included, non-negotiable
- Checkpoint — always included
- Conversation buffer — as much as fits, most recent first
- Retrieved knowledge — as much as fits, most relevant first

#### 3. Token Counter
- Estimates token usage of the current assembled context
- Uses tiktoken or similar for fast local counting
- Tracks cumulative usage during a session (system + all message pairs)
- When usage crosses threshold (configurable, default ~70% of model context window), flags the session for handover
- Does NOT interrupt the current exchange — flags only

#### 4. Session Manager
Orchestrates the invisible handover:
1. Token counter flags session approaching limit
2. After the current response is delivered to the user:
   a. Save the full session transcript as gzipped JSONL to `data/transcripts/`
   b. End the session in the database
   c. Fire the Curator as a background task (`asyncio.create_task`) — UI unblocks immediately
   d. Curator extracts knowledge entries (with optional `source_ref` to transcript) and updated checkpoint
   e. Writes extracted data to the knowledge store and checkpoint table
3. The user's next message starts a fresh API session — Context Assembler builds from scratch
4. The conversation buffer ensures the last N messages bridge the gap

**Critical requirement:** The user must never notice the handover. The curator runs in the background so the UI is never blocked during extraction.

#### 5. Curator
A separate API call using Opus for high-quality extraction.

**Input:** The current conversation context (or the portion since last curation)

**Prompt instructs the curator to extract:**
- Facts about the user, their life, environment
- Preferences, views, and tastes
- Decisions with reasoning
- Corrections to previously stored knowledge
- Rejected approaches (with reasons why they failed)
- Events and milestones
- Project details and architecture
- Actions — file edits, implementations, configuration changes made during the session
- Updated checkpoint summarising current state

Each entry is assigned two **retention dimensions**:
- **continuity** (HIGH/LOW): Is this needed to resume current work? HIGH for implementation details, file changes, active bugs, where we left off. Decays once the project/task is resolved.
- **durable** (HIGH/LOW): Does this matter about the user long-term? HIGH for life events, personal facts, preferences, corrections. Doesn't decay.

**Output format:** Structured JSON entries, each with:
```json
{
  "type": "fact|preference|decision|correction|rejected|event|project|action",
  "category": "short consistent label",
  "content": "the actual knowledge entry",
  "continuity": "high|low",
  "durable": "high|low",
  "event_date": "YYYY-MM-DD or null",
  "source_ref": {"from_msg": N, "to_msg": M} or null
}
```

**Checkpoint output:**
```json
{
  "summary": "Current state of ongoing discussions...",
  "active_topics": ["topic1", "topic2"]
}
```

**Source references:** The curator prompt numbers each message with a sequential index (`[0]`, `[1]`, etc.). When an entry summarises reasoning, detailed discussion, or theory where the full context would be valuable, the curator adds a `source_ref` with the message range. The save loop combines this with the transcript filename to build the full reference stored in the knowledge table.

**Curator model:** Opus is the default and recommended model. The quality of extraction is the critical bottleneck for the entire memory system — this is not the place to economise.

#### 6. Knowledge Store
SQLite tables. Each entry tagged with user_id, type, category (topic), content, continuity, durable, event_date, timestamps, and supersedes references.

**Key design principle:** Nothing gets deleted. Superseded entries get marked as superseded with a reference to what replaced them. Failed approaches stay forever with their failure reasons. This is the "map of where the mines are buried."

ChromaDB with sentence-transformers (all-MiniLM-L6-v2) handles retrieval via semantic vector search. Knowledge entries are embedded on creation and searched by cosine similarity at query time. SQLite FTS5 indexes are maintained in the schema but are not used for context assembly retrieval.

#### 7. Web Search
When enabled, Claude can search the web in real-time using Anthropic's built-in web search tool (powered by Brave Search). Results include citations rendered as clickable links. Configurable and optional.

#### 8. URL Reading
Claude can fetch and read web pages directly when you share a URL. Useful for discussing articles, documentation, or any web content without copy-pasting.

#### 9. Local File Reading
Claude can read files from your local machine when you provide a path. Supports optional `from_line`/`to_line` parameters to read a specific line range instead of the whole file. Gzip files (`.gz`) are decompressed transparently — this is how Claude reads session transcripts.

#### 10. Session Transcripts
Full session transcripts are saved as gzipped JSONL in `data/transcripts/` at handover time (before curation). Each line: `{"index": N, "role": "user"|"assistant", "content": "..."}`. The index is sequential among user/assistant messages only. Filename format: `session_YYYY-MM-DD_HH-MM.txt.gz` (from the first message timestamp).

Knowledge entries with a `source_ref` point to a specific message range in a transcript file. The system prompt instructs Claude to use `read_file` with `from_line`/`to_line` matching `from_msg`/`to_msg` to retrieve the original context on demand.

#### 11. Image Input
Users can attach one image per message via paste, drag-and-drop, or file upload. The image is sent to the Anthropic API as a base64 content block alongside the text. Supported types: JPEG, PNG, GIF, WebP (max 10 MB). Images are stored in the messages table (`image_data`, `image_media_type` columns) and rendered inline in conversation history. Only the new message includes the image — buffer messages exclude images to conserve token budget.

---

### Data Model

```sql
-- Users
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Personas (one active per user, history preserved)
CREATE TABLE personas (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    persona_text TEXT NOT NULL,
    active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Conversation log (every message ever sent/received)
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    session_id TEXT NOT NULL,  -- groups messages within an API session
    token_estimate INTEGER,
    image_data TEXT,           -- base64-encoded image (user messages only)
    image_media_type TEXT,     -- e.g. "image/png", "image/jpeg"
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Knowledge store
CREATE TABLE knowledge (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    type TEXT NOT NULL CHECK(type IN ('fact', 'preference', 'decision', 'correction', 'rejected', 'event', 'project', 'action')),
    topic TEXT NOT NULL,
    content TEXT NOT NULL,
    continuity TEXT DEFAULT 'low' CHECK(continuity IN ('high', 'low')),
    durable TEXT DEFAULT 'low' CHECK(durable IN ('high', 'low')),
    event_date TEXT,
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'retired')),
    supersedes_id INTEGER REFERENCES knowledge(id),
    source_session_id TEXT,  -- which session this was extracted from
    source_ref TEXT,         -- JSON: {"file": "...", "from_msg": N, "to_msg": M}
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Full-text search index on knowledge (legacy — kept in schema but not used for retrieval;
-- ChromaDB vector search is the primary retrieval mechanism)
CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    topic, content, content=knowledge, content_rowid=id
);

-- Checkpoints (one active per user)
CREATE TABLE checkpoints (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    summary TEXT NOT NULL,
    active_topics TEXT,  -- JSON array
    active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Session metadata
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,  -- UUID
    user_id INTEGER NOT NULL REFERENCES users(id),
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    end_reason TEXT CHECK(end_reason IN ('token_limit', 'manual', 'timeout', 'error')),
    tokens_used INTEGER
);
```

---

### Multi-User Design

- Simple login page: pick username or enter new one
- Session cookies for browser persistence
- All queries scoped by user_id
- Each user has independent: persona, conversation history, knowledge store, checkpoint
- Curator runs per-user
- Frontend shows only that user's conversation history

---

### Persona Files

Each user gets a persona that tells Claude who it's talking to and how to behave.

**Example — Technical discussion persona:**
```
You are engaged in an ongoing conversation with [Name] about software development,
technology, and related topics. [Name] is an experienced developer with particular
interests in [areas]. They have strong opinions, appreciate direct disagreement,
and dislike obvious flattery.

Treat this as a continuing conversation between colleagues. Be opinionated.
Push back when you disagree. Reference previous discussions naturally.
```

**Example — Business advisor persona:**
```
You are a business advisor in an ongoing conversation with [Name] about their
e-commerce businesses. Be practical and direct. Remember previous business
discussions, decisions made, and ideas explored (including ones that were
rejected and why). Focus on actionable advice.
```

Personas are editable through the UI settings page.

---

### Configuration

```yaml
# config.yaml
server:
  host: "0.0.0.0"  # listen on all interfaces for network access
  port: 8080

anthropic:
  api_key: "${MEMCHAT_API_KEY}"  # env var, never in config file
  conversation_model: "claude-opus-4-6-20250610"   # main chat model
  curator_model: "claude-opus-4-6-20250610"        # extraction model (Opus recommended)
  max_context_tokens: 200000       # model context window
  handover_threshold: 0.70         # trigger curator at 70% usage

conversation:
  buffer_messages: 20              # recent messages to carry across handovers
  max_knowledge_entries: 30        # max retrieved knowledge entries per context

database:
  path: "./data/memchat.db"        # SQLite database location
```

---

### Project Structure

```
memchat/
├── README.md
├── PROJECT_SPEC.md          # this file
├── requirements.txt
├── config.yaml
├── src/
│   ├── __init__.py
│   ├── main.py              # FastAPI app entry point
│   ├── config.py            # configuration loading
│   ├── database.py          # SQLite setup, migrations, queries
│   ├── models.py            # Pydantic models for API/internal data
│   ├── auth.py              # simple user auth/session management
│   ├── assembler.py         # context assembly logic
│   ├── counter.py           # token counting
│   ├── curator.py           # knowledge extraction prompts and processing
│   ├── knowledge.py         # knowledge store queries and management
│   ├── anthropic_client.py  # Anthropic API wrapper
│   ├── system_prompt.py     # hardcoded system prompt (not user-editable)
│   ├── transcript.py        # session transcript storage (gzipped JSONL)
│   ├── file_read.py         # local file reading (with gzip and line-range support)
│   ├── url_fetch.py         # URL fetching capability
│   ├── vector_store.py      # ChromaDB vector search for knowledge retrieval
│   └── routes/
│       ├── __init__.py
│       ├── chat.py          # chat endpoints (send message, get history)
│       ├── debug.py         # debug page (knowledge entries, checkpoints, sessions)
│       ├── users.py         # user management endpoints
│       └── settings.py      # persona editing, config viewing
├── static/
│   ├── css/
│   │   └── style.css
│   ├── js/
│   │   └── chat.js
│   └── img/
├── templates/
│   ├── base.html
│   ├── chat.html
│   ├── login.html
│   └── settings.html
├── tests/
│   ├── test_assembler.py
│   ├── test_curator.py
│   ├── test_counter.py
│   └── test_knowledge.py
└── data/                    # local data (gitignored)
    ├── immortalchat.db      # SQLite database
    ├── chromadb/             # ChromaDB vector store
    └── transcripts/          # gzipped session transcripts
```

---

### Known Limitations

- **Knowledge retrieval** uses ChromaDB vector search for semantic matching. Retrieval quality with very large knowledge stores (thousands of entries) is untested.
- **Curator quality** is critical. Opus is the default and recommended model.
- **Checkpoint drift** is theoretically possible over many months of rewrites. Not yet observed in practice.
- **No mobile app.** Use the browser on your phone — it works fine.
- **Passwords are basic.** SHA-256, no salt. This is "don't accidentally open each other's chat" security, not "defend against attackers" security.

---

### Open Questions

1. **Knowledge decay:** Do old, unreferenced knowledge entries eventually get deprioritised in retrieval? Or is everything equal? Currently everything equal.

2. **Conversation buffer size:** 20 messages is a starting point. May need tuning. Too few and Claude loses conversational thread. Too many and we waste context on old chat instead of knowledge.

---

### Non-Goals

- Mobile app (browser on phone works fine)
- Cloud deployment
- End-to-end encryption (it's your local network)
- Voice input/output
- Image generation (image input/vision is supported; generation is not)
- Plugin system

---

### Name

**Memchat** — chat with memory.
