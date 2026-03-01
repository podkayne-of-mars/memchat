"""Pydantic models for Immortal Chat API and internal data."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


# --- Enums ---

class MessageRole(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class KnowledgeType(str, Enum):
    fact = "fact"
    opinion = "opinion"
    decision = "decision"
    correction = "correction"
    failed_approach = "failed_approach"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class KnowledgeStatus(str, Enum):
    active = "active"
    superseded = "superseded"
    retired = "retired"


class SessionEndReason(str, Enum):
    token_limit = "token_limit"
    manual = "manual"
    timeout = "timeout"
    error = "error"


# --- Database record models ---

class User(BaseModel):
    id: int
    username: str
    display_name: str
    created_at: datetime | None = None


class Persona(BaseModel):
    id: int
    user_id: int
    persona_text: str
    active: bool = True
    created_at: datetime | None = None


class Message(BaseModel):
    id: int
    user_id: int
    role: MessageRole
    content: str
    session_id: str
    token_estimate: int | None = None
    created_at: datetime | None = None


class KnowledgeEntry(BaseModel):
    id: int
    user_id: int
    type: KnowledgeType
    topic: str
    content: str
    confidence: Confidence = Confidence.medium
    status: KnowledgeStatus = KnowledgeStatus.active
    supersedes_id: int | None = None
    source_session_id: str | None = None
    created_at: datetime | None = None


class Checkpoint(BaseModel):
    id: int
    user_id: int
    summary: str
    active_topics: str | None = None
    active: bool = True
    created_at: datetime | None = None


class Session(BaseModel):
    id: str
    user_id: int
    started_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: SessionEndReason | None = None
    tokens_used: int | None = None


# --- Request / Response models ---

class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=100)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    message_id: int


class PersonaUpdate(BaseModel):
    persona_text: str = Field(min_length=1)
