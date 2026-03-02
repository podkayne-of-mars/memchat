# Memchat

A local chat client that gives Claude permanent memory. You chat, it remembers. Forever.

## Status: Alpha

This is an early-stage experiment in persistent AI memory. The core memory loop works but has not been extensively tested at scale, has rough edges, and will evolve. Use it, break it, improve it. That's why it's here.

## How This Was Built

Memchat was vibe-coded. The architecture, design decisions, and direction are human. Every line of code was written by Claude Code, with a Claude Desktop instance acting as project manager. No human developer has reviewed the code line by line.

## What It's Designed To Do

Memchat is a local web-based chat application designed to create the illusion of a continuous, never-ending conversation with Claude. There is no "new chat" button. The conversation should never clear, never degrade, and never forget.

Behind the scenes, the system manages Claude's context window invisibly. When the context fills up, a separate AI call silently extracts important information — facts, preferences, decisions, corrections, rejected approaches, events, project details, and session actions — into a local database. Each entry is tagged with type, category, date, and two retention dimensions: continuity (needed for current work) and durable (matters long-term). The next message starts a fresh API session, rebuilt from stored knowledge, a conversation checkpoint, and recent message history. Ideally, the user doesn't notice the transition.

Over time, the AI should accumulate genuine understanding of you: your preferences, your projects, your decisions, and — critically — the things you tried that didn't work. The goal is that it gets more useful the longer you use it.

## Architecture

```
┌─────────────────────────────────────┐
│         Browser (any device)         │
│        http://localhost:8080         │
└──────────────────┬──────────────────┘
                   │
┌──────────────────┴──────────────────┐
│        FastAPI Backend (Python)      │
│                                      │
│  ┌────────────┐  ┌───────────────┐  │
│  │  Context    │  │    Session    │  │
│  │  Assembler  │  │   Manager    │  │
│  └─────┬──────┘  └───────┬──────┘  │
│        │                  │         │
│  ┌─────┴──────────────────┴──────┐  │
│  │         Token Counter         │  │
│  └───────────────┬───────────────┘  │
│                  │                   │
│  ┌───────────────┴───────────────┐  │
│  │           Curator             │  │
│  │   (separate AI call — Opus)   │  │
│  └───────────────┬───────────────┘  │
│                  │                   │
│  ┌───────────────┴───────────────┐  │
│  │     SQLite + ChromaDB         │  │
│  │  messages │ knowledge │ users │  │
│  └───────────────────────────────┘  │
└──────────────────┬──────────────────┘
                   │ HTTPS
                   ▼
           Anthropic API (Claude)
```

### How the Memory Loop Works

**Normal chat:** User sends a message. The Context Assembler builds an API payload from the user's persona, the current checkpoint summary, relevant knowledge entries retrieved from the database, and the last ~20 messages for conversational flow. Claude responds. Both messages are stored.

**Handover (invisible to user):** The Token Counter monitors context usage. When it crosses a configurable threshold (default 70%), the system silently triggers the Curator — a separate API call (Opus recommended) that extracts structured knowledge from the conversation:

| Type | What It Captures | Example |
|------|-----------------|---------|
| Fact | Stated truths | "Has visited Tokyo three times" |
| Preference | Views, tastes, and preferences | "Prefers Heinlein over Asimov" |
| Decision | Choices with reasoning | "Chose SQLite over Postgres for simplicity" |
| Correction | Updated information | "Actually three visits, not two" |
| Rejected | Ideas tried and abandoned, with reasons | "Considered Redis, rejected as overkill" |
| Event | Life events and milestones | "Got a new job at Acme Corp" |
| Project | Ongoing project details | "Memchat uses ChromaDB for vector search" |
| Action | Session changes — file edits, implementations | "Added continuity/durable columns to knowledge table" |

Each entry is tagged with type, category, date, and two retention dimensions: **continuity** (HIGH/LOW — needed to resume current work, decays when resolved) and **durable** (HIGH/LOW — matters about the user long-term, doesn't decay). Nothing is ever deleted — superseded entries are marked as such, preserving a full audit trail. Rejected approaches are explicitly preserved because they're the most expensive knowledge to lose.

The Curator also writes a narrative checkpoint — a 2-4 sentence summary of the current conversation state.

The next user message starts a fresh API session. The Context Assembler rebuilds from: persona + checkpoint + relevant knowledge entries (via ChromaDB vector search) + recent messages. The user never notices the transition.

### Knowledge That Persists

The knowledge store is not a flat log. It's structured:

- **Active** entries are retrieved and injected into context
- **Superseded** entries are preserved but not retrieved — they link to what replaced them
- **Retired** entries are hidden but never deleted
- **Continuity** (HIGH/LOW) flags entries needed to resume current work — decays when the task is resolved
- **Durable** (HIGH/LOW) flags entries that matter about the user long-term — doesn't decay
- **Rejected** entries include rejection reasons, preventing the AI from re-suggesting dead ends

### Memory Surfacing

The AI doesn't just passively hold knowledge — it actively surfaces it. When entries with HIGH continuity or HIGH durable are relevant to the user's message, the AI begins its response with a brief "What I already know" section. This proves it remembers, gives the user a chance to correct stale info, and prevents the AI from ignoring its own stored knowledge. Before web searches, surfacing is mandatory — the AI must show what it already knows before reaching for the web.

### Web Search

When enabled, Claude can search the web in real-time using Anthropic's built-in web search tool (powered by Brave Search). Results include citations rendered as clickable links. Configurable and optional — costs approximately $0.01 per search on top of normal token costs.

### URL Reading

Claude can fetch and read web pages directly when you share a URL. Useful for discussing articles, documentation, or any web content without copy-pasting.

### Local File Reading

Claude can read files from your local machine when you provide a path. Useful for reviewing code, configs, logs, or documents without pasting content into chat.

### Image Support

Paste, drag-and-drop, or upload images directly in chat. Claude sees the image and can describe, analyse, or answer questions about it. Supports JPEG, PNG, GIF, and WebP up to 10 MB. Images are stored in the database and displayed in conversation history on reload. One image per message; text is always required alongside the image.

### Multi-User

Multiple users share a single instance with complete data separation. Each user has their own persona, conversation history, knowledge store, and checkpoint. Simple password authentication — designed for household use on a local network, not enterprise security.

## Requirements

- Python 3.10+
- Anthropic API key (pay-as-you-go, approximately $1-3/day for active use)

## Install and Run

```bash
git clone https://github.com/podkayne-of-mars/memchat.git
cd memchat
pip install -r requirements.txt
cp config.yaml.example config.yaml
# Edit config.yaml if needed
export ANTHROPIC_API_KEY="your-key-here"
python -m uvicorn src.main:app --host 127.0.0.1 --port 8080
```

Open http://localhost:8080 in your browser. Create a user. Start chatting.

For network access from other devices, use `--host 0.0.0.0`.

## Configuration

Key settings in `config.yaml`:

| Setting | Default | What It Does |
|---------|---------|-------------|
| `conversation_model` | `claude-opus-4-6-20250610` | Model for chat |
| `curator_model` | `claude-opus-4-6-20250610` | Model for knowledge extraction |
| `max_context_tokens` | `200000` | Context window size |
| `handover_threshold` | `0.70` | Trigger curator at this % of context |
| `buffer_messages` | `20` | Recent messages carried across handovers |
| `web_search` | `true` | Enable web search capability |

## Known Limitations

- **Knowledge retrieval** uses ChromaDB vector search (sentence-transformers all-MiniLM-L6-v2) for semantic matching. Scales well, but retrieval quality with very large knowledge stores (thousands of entries) is untested.
- **Curator quality** is critical. Opus is the default and recommended model. The debug page lets you inspect and retire bad entries.
- **Checkpoint drift** is theoretically possible over many months of rewrites. Not yet observed in practice.
- **Passwords are basic.** SHA-256, no salt. This is "don't accidentally open each other's chat" security, not "defend against attackers" security. Don't reuse a real password.

## API Costs

Typical usage with Opus for both chat and curator:

| Activity | Approximate Cost |
|----------|-----------------|
| Single chat exchange | $0.05 - $0.15 |
| Curator extraction (Opus) | $0.02 - $0.05 |
| Web search | $0.01 per search |
| Active daily use (one person) | $5 - $15 |
| Monthly (two-person household) | $150 - $400 |

## License

GPL v3. If you improve it, share it back. See LICENSE for details.

## Contributing

This started as a weekend project. If you find it useful, find bugs, or want to improve it — PRs welcome. The full technical specification is in PROJECT_SPEC.md.