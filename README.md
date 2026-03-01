# Immortal Chat

A local desktop chat app with invisible persistent memory. It uses the Anthropic API (Claude) and automatically extracts knowledge from your conversations, so the AI remembers what you've discussed across sessions — preferences, decisions, context, even things you tried and rejected.

Multi-user, local-first, no cloud storage. Built for a household where two people share one machine.

## How it works

1. You chat with Claude through a local web UI
2. When a session's token usage crosses a threshold, a curator model (Haiku) extracts knowledge — facts, opinions, decisions, corrections, failed approaches
3. That knowledge is stored in a local SQLite database with FTS5 full-text search
4. Next time you chat, relevant knowledge is retrieved and injected into the system prompt
5. Claude responds as if it remembers everything

Each user gets their own session history, knowledge store, persona, and checkpoints.

## Requirements

- Python 3.12+
- An [Anthropic API key](https://console.anthropic.com/)

## Install

```bash
git clone https://github.com/youruser/immortalchat.git
cd immortalchat
pip install -r requirements.txt
cp config.yaml.example config.yaml
```

Set your API key as an environment variable:

```bash
# Linux / macOS
export ANTHROPIC_API_KEY="sk-ant-..."

# Windows
setx ANTHROPIC_API_KEY "sk-ant-..."
```

## Run

```bash
python -m uvicorn src.main:app --host 127.0.0.1 --port 8080
```

Then open [http://127.0.0.1:8080](http://127.0.0.1:8080) in your browser. You'll be prompted to create a user on first visit.

## Passwords

Passwords are basic — this is designed for local network use to prevent users accidentally accessing each other's conversations. Don't reuse a real password.

## Configuration

Edit `config.yaml` to change models, token thresholds, buffer sizes, etc. See `config.yaml.example` for all available options. The API key is always read from the `ANTHROPIC_API_KEY` environment variable — it is never stored in the config file.

## License

[GPL v3](LICENSE)
