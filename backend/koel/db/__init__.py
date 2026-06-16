"""Persistence layer: PostgreSQL + pgvector (PRD §07).

A single database holds relational session/call data and the RAG vector store,
so MVP needs no separate vector service.
"""

from koel.db.session import get_engine, get_sessionmaker, session_scope

__all__ = ["get_engine", "get_sessionmaker", "session_scope"]
