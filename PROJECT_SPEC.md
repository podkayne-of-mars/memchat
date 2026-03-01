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
2. After the current response is delivered to the user, BEFORE processing the next user message:
   a. Send current session context to the Curator
   b. Curator extracts knowledge entries and updated checkpoint
   c. Write extracted data to the knowledge store and checkpoint table
   d. Start a fresh API session — Context Assembler builds from scratch
3. The user's next message gets a fresh, clean context with curated knowledge
4. The conversation buffer ensures the last N messages bridge the gap

**Critical requirement:** The user must never notice the handover. Response time should feel normal. The curator call happens in the background or is fast enough to be imperceptible.

#### 5. Curator
A separate API call using Opus for high-quality extraction.

**Input:** The current conversation context (or the portion since last curation)

**Prompt instructs the curator to extract:**
- New factual claims, opinions, or preferences expressed by the user
- New factual claims or conclusions from Claude responses
- Corrections to previously stored knowledge
- Failed approaches or rejected ideas (with reasons)
- Decisions made
- Updated checkpoint summarising current state

**Output format:** Structured JSON entries, each with:
```json
{
  "type": "fact|opinion|decision|correction|failed_approach",
  "topic": "short topic label",
  "content": "the actual knowledge entry",
  "confidence": "high|medium|low",
  "date": "2026-03-01",
  "supersedes": null  // or ID of entry this replaces
}
```

**Checkpoint output:**
```json
{
  "summary": "Current state of ongoing discussions...",
  "active_topics": ["topic1", "topic2"],
  "updated_at": "2026-03-01T12:00:00"
}
```

**Curator model:** Opus is recommended. Haiku is cheaper but tends to flatten nuance and lose subtle reasoning. The quality of extraction is the critical bottleneck for the entire memory system — this is not the place to economise.

#### 6. Knowledge Store
SQLite tables. Each entry tagged with user_id, type, topic, content, confidence, timestamps, and supersedes references.

**Key design principle:** Nothing gets deleted. Superseded entries get marked as superseded with a reference to what replaced them. Failed approaches stay forever with their failure reasons. This is the "map of where the mines are buried."

ChromaDB with sentence-transformers (all-MiniLM-L6-v2) handles retrieval via semantic vector search. Knowledge entries are embedded on creation and searched by cosine similarity at query time. SQLite FTS5 indexes are maintained in the schema but are not used for context assembly retrieval.

#### 7. Web Search
When enabled, Claude can search the web in real-time using Anthropic's built-in web search tool (powered by Brave Search). Results include citations rendered as clickable links. Configurable and optional.

#### 8. URL Reading
Claude can fetch and read web pages directly when you share a URL. Useful for discussing articles, documentation, or any web content without copy-pasting.

#### 9. Local File Reading
Claude can read files from your local machine when you provide a path. Useful for reviewing code, configs, logs, or documents without pasting content into chat.

#### 10. Image Input
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
    type TEXT NOT NULL CHECK(type IN ('fact', 'opinion', 'decision', 'correction', 'failed_approach')),
    topic TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence TEXT DEFAULT 'medium' CHECK(confidence IN ('high', 'medium', 'low')),
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'retired')),
    supersedes_id INTEGER REFERENCES knowledge(id),
    source_session_id TEXT,  -- which session this was extracted from
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
  api_key: "${ANTHROPIC_API_KEY}"  # env var, never in config file
  conversation_model: "claude-opus-4-5-20251101"   # main chat model
  curator_model: "claude-opus-4-5-20251101"        # extraction model (Opus recommended)
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
│   ├── file_read.py         # local file reading capability
│   ├── url_fetch.py         # URL fetching capability
│   ├── vector_store.py      # ChromaDB vector search for knowledge retrieval
│   └── routes/
│       ├── __init__.py
│       ├── chat.py          # chat endpoints (send message, get history)
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
└── data/                    # SQLite DB lives here (gitignored)
    └── .gitkeep
```

---

### Known Limitations

- **Knowledge retrieval** uses ChromaDB vector search for semantic matching. Retrieval quality with very large knowledge stores (thousands of entries) is untested.
- **Curator quality** is critical. Opus is recommended — Haiku tends to flatten nuance.
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
