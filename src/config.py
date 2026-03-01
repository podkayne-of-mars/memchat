"""Configuration loading for Immortal Chat."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class AnthropicConfig:
    api_key: str = ""
    conversation_model: str = "claude-sonnet-4-20250514"
    curator_model: str = "claude-haiku-4-5-20251001"
    max_context_tokens: int = 200_000
    handover_threshold: float = 0.70


@dataclass
class ConversationConfig:
    buffer_messages: int = 20
    max_knowledge_entries: int = 30


@dataclass
class CuratorConfig:
    backend: str = "anthropic"
    local_model: str = "mistral"


@dataclass
class DatabaseConfig:
    path: str = "./data/immortalchat.db"


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    conversation: ConversationConfig = field(default_factory=ConversationConfig)
    curator: CuratorConfig = field(default_factory=CuratorConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)


def _build_section(cls, data: dict | None):
    """Build a dataclass from a dict, ignoring unknown keys."""
    if not data:
        return cls()
    valid = {f.name for f in cls.__dataclass_fields__.values()}
    return cls(**{k: v for k, v in data.items() if k in valid})


def load_config(config_path: str = "config.yaml") -> AppConfig:
    """Load configuration from YAML file with env-var overrides."""
    path = Path(config_path)
    raw: dict = {}
    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}

    cfg = AppConfig(
        server=_build_section(ServerConfig, raw.get("server")),
        anthropic=_build_section(AnthropicConfig, raw.get("anthropic")),
        conversation=_build_section(ConversationConfig, raw.get("conversation")),
        curator=_build_section(CuratorConfig, raw.get("curator")),
        database=_build_section(DatabaseConfig, raw.get("database")),
    )

    # API key always comes from environment
    cfg.anthropic.api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    return cfg


# Singleton — loaded once at import, accessible everywhere
_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config
