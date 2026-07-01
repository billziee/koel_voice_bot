"""Bootstrap the database for local/demo (PRD §10 steps 2 & 4).

Creates the pgvector extension, all tables, then seeds:
  - the Dummy CRM customers (all four use cases + a DND record),
  - three demo bot instances (HDFC / ICICI / Insurance, per PRD §09),
  - a small RAG knowledge base for the credit-card use case.

Run: python -m scripts.bootstrap_db
"""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, text

from koel.crm.seed import SEED_CUSTOMERS
from koel.db.models import Base, BotConfig, CrmCustomer, KnowledgeChunk, UseCase
from koel.db.session import get_engine, session_scope
from koel.nlp.config import get_nlp_settings
from koel.nlp.rag import build_embedder

DEMO_BOTS = [
    {
        "id": "bot_hdfc_cc",
        "name": "HDFC Credit Card",
        "use_case": UseCase.credit_card,
        "language": "hinglish",
        "default_tone": "cheerful",
        "rag_index": "cc_hdfc",
        "greeting_template": "Namaste {name} ji, main HDFC Bank ki taraf se KOEL bol rahi hoon.",
    },
    {
        "id": "bot_icici_col",
        "name": "ICICI Collections",
        "use_case": UseCase.collections,
        "language": "hinglish",
        "default_tone": "empathetic",
        "rag_index": "col_icici",
        "greeting_template": "Namaste {name} ji, main ICICI Bank se baat kar rahi hoon.",
    },
    {
        "id": "bot_insurance",
        "name": "Insurance Bot",
        "use_case": UseCase.insurance,
        "language": "hi",
        "default_tone": "neutral",
        "rag_index": "ins_term",
        "greeting_template": "Namaste {name} ji, main Devotica Life se baat kar rahi hoon.",
    },
]

# Credit-card RAG knowledge (objection handling + product facts), PRD §04/§06.
KB_CC: list[dict] = [
    {
        "title": "Objection: already have a card",
        "content": (
            "Yeh card lounge access aur 4x reward points deta hai dining par — "
            "aapke existing cards se alag benefit. Pehle saal ki fee bhi waived hai."
        ),
    },
    {
        "title": "Product: Regalia features",
        "content": "Regalia: airport lounge access, 4x dining rewards, zero first-year fee, fuel waiver.",
    },
    {
        "title": "Objection: annual fee",
        "content": "Pehle saal ki annual fee waived hai, aur agle saal se spend-based waiver milta hai.",
    },
]


async def main() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
        # HNSW index for sub-100ms cosine ANN at scale (idempotent; pgvector >= 0.5).
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_hnsw "
                "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
            )
        )

    embedder = build_embedder(get_nlp_settings())

    async with session_scope() as db:
        # CRM customers
        for record in SEED_CUSTOMERS:
            if await db.get(CrmCustomer, record["id"]) is None:
                db.add(CrmCustomer(**record))

        # Bot instances
        for bot in DEMO_BOTS:
            if await db.get(BotConfig, bot["id"]) is None:
                db.add(BotConfig(**bot))

        # Knowledge base: refresh so embeddings always match the current embedder
        # (switching hash → nvidia requires re-embedding). Chunks are "passages".
        await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.rag_index == "cc_hdfc"))
        for chunk in KB_CC:
            db.add(
                KnowledgeChunk(
                    rag_index="cc_hdfc",
                    use_case=UseCase.credit_card,
                    language="hinglish",
                    title=chunk["title"],
                    content=chunk["content"],
                    embedding=await embedder.embed(
                        chunk["title"] + " " + chunk["content"], input_type="passage"
                    ),
                )
            )

    aclose = getattr(embedder, "aclose", None)
    if aclose is not None:
        await aclose()
    print("Bootstrap complete: tables created, CRM + bots + knowledge base seeded.")


if __name__ == "__main__":
    asyncio.run(main())
