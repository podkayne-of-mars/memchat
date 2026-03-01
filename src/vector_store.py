"""ChromaDB vector store for semantic knowledge search."""

import logging

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from src.config import get_config
from src.database import get_all_active_knowledge, get_connection

logger = logging.getLogger(__name__)

_client: chromadb.ClientAPI | None = None
_collection: chromadb.Collection | None = None


def init_vector_store() -> None:
    """Initialise ChromaDB with persistent storage and migrate existing entries."""
    global _client, _collection

    ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    _client = chromadb.PersistentClient(path="data/chromadb")
    _collection = _client.get_or_create_collection(
        name="knowledge",
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )

    # Migration: if collection is empty, bulk-add all active entries from SQLite
    if _collection.count() == 0:
        _migrate_existing_entries()


def _migrate_existing_entries() -> None:
    """Bulk-add all active knowledge entries from SQLite into ChromaDB."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, user_id, topic, content FROM knowledge WHERE status = 'active'"
        ).fetchall()

    if not rows:
        logger.info("Vector store migration: no active entries to migrate.")
        return

    ids = [str(r["id"]) for r in rows]
    documents = [f"{r['topic']}: {r['content']}" for r in rows]
    metadatas = [{"user_id": r["user_id"], "status": "active"} for r in rows]

    _collection.add(ids=ids, documents=documents, metadatas=metadatas)
    logger.info("Vector store migration: embedded %d entries.", len(ids))


def add_knowledge(entry_id: int, user_id: int, topic: str, content: str) -> None:
    """Upsert a single knowledge entry into the vector store."""
    if _collection is None:
        return
    _collection.upsert(
        ids=[str(entry_id)],
        documents=[f"{topic}: {content}"],
        metadatas=[{"user_id": user_id, "status": "active"}],
    )


def retire_knowledge(entry_id: int) -> None:
    """Mark a single entry as retired in the vector store."""
    if _collection is None:
        return
    _collection.update(
        ids=[str(entry_id)],
        metadatas=[{"status": "retired"}],
    )


def retire_all_knowledge(user_id: int) -> None:
    """Mark all entries for a user as retired in the vector store."""
    if _collection is None:
        return
    results = _collection.get(
        where={"$and": [{"user_id": user_id}, {"status": "active"}]},
    )
    if not results["ids"]:
        return
    _collection.update(
        ids=results["ids"],
        metadatas=[{"status": "retired"} for _ in results["ids"]],
    )


def search_knowledge(
    user_id: int,
    query: str,
    n_results: int = 30,
    min_score: float | None = None,
) -> list[int]:
    """Semantic search for knowledge entries. Returns entry IDs ordered by similarity.

    ChromaDB cosine distance: similarity = 1 - distance.
    Entries below min_score are filtered out.
    """
    if _collection is None:
        return []

    if min_score is None:
        min_score = get_config().conversation.similarity_threshold

    # ChromaDB throws if n_results exceeds matching document count
    total = _collection.count()
    if total == 0:
        return []
    n_results = min(n_results, total)

    results = _collection.query(
        query_texts=[query],
        n_results=n_results,
        where={"$and": [{"user_id": user_id}, {"status": "active"}]},
    )

    ids: list[int] = []
    distances = results["distances"][0] if results["distances"] else []
    raw_ids = results["ids"][0] if results["ids"] else []

    for doc_id, distance in zip(raw_ids, distances):
        similarity = 1.0 - distance
        if similarity >= min_score:
            ids.append(int(doc_id))

    return ids
