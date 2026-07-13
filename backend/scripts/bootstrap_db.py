"""Bootstrap the database for local/demo (PRD §10 steps 2 & 4).

Schema is owned by Alembic (O) — this runs ``alembic upgrade head`` (which also
creates the pgvector extension + HNSW index), then seeds:
  - the tenant registry + login users (demo platform-admin + two org operators),
  - the Dummy CRM customers (all four use cases + a DND record),
  - three demo bot instances (HDFC / ICICI / Insurance, per PRD §09),
  - a small RAG knowledge base for the credit-card use case.

Run: python -m scripts.bootstrap_db
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from sqlalchemy import delete

from koel.crm.seed import SEED_CUSTOMERS
from koel.db.models import BotConfig, CrmCustomer, KnowledgeChunk, UseCase
from koel.db.seed_users import seed_orgs, seed_users
from koel.db.session import session_scope
from koel.nlp.config import get_nlp_settings
from koel.nlp.rag import build_embedder

# Backend root (contains alembic.ini) so migrations run regardless of CWD.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _run_migrations() -> None:
    """Apply Alembic migrations synchronously before the async seed phase.

    Run as a subprocess (not alembic.command) because env.py drives its own
    asyncio loop, which cannot be nested inside this script's asyncio.run.
    """
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_BACKEND_ROOT,
        check=True,
    )

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
        "greeting_template": "नमस्ते {name} जी, मैं Devotica Life की ओर से बात कर रही हूँ।",
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

# Collections (pre-due) RAG knowledge — romanized Hinglish (ICICI bot → Orpheus).
KB_COL: list[dict] = [
    {
        "title": "Objection: already paid",
        "content": (
            "Agar aapne payment kar di hai to dhanyavaad — system mein reflect hone "
            "mein 24-48 ghante lag sakte hain. Reference number share kar dein to "
            "main note kar leti hoon."
        ),
    },
    {
        "title": "Partial payment offer",
        "content": (
            "Agar poora amount abhi possible nahi hai, aap partial payment kar sakte "
            "hain — isse late fee aur credit score par asar kam hota hai."
        ),
    },
    {
        "title": "Payment options and due date",
        "content": (
            "Aap UPI, net banking ya payment link se pay kar sakte hain. Due date se "
            "pehle payment par koi late fee nahi lagti."
        ),
    },
]

# Life-insurance (IRDAI) RAG knowledge — Devanagari Hindi (Insurance bot → Magpie).
KB_INS: list[dict] = [
    {
        "title": "Product: term plan basics",
        "content": (
            "टर्म लाइफ प्लान में प्रीमियम पूरी पॉलिसी अवधि के लिए फिक्स रहता है — उम्र या "
            "क्लेम के साथ नहीं बढ़ता। जल्दी लेने पर प्रीमियम कम रहता है।"
        ),
    },
    {
        "title": "Objection: premium too high",
        "content": (
            "प्रीमियम आपकी उम्र और कवर राशि पर निर्भर करता है। उपयुक्त कवर या लंबी अवधि "
            "चुनकर प्रीमियम को किफायती बनाया जा सकता है।"
        ),
    },
    {
        "title": "Nominee and IRDAI disclosure",
        "content": (
            "नॉमिनी क्लेम राशि का हकदार होता है। यह एक IRDAI-अनुमोदित प्लान है; बाध्यकारी "
            "चरणों के लिए आपको एक लाइसेंस प्राप्त सलाहकार से जोड़ा जाएगा।"
        ),
    },
]

# (rag_index, use_case, language, chunks) — every configured bot's KB is grounded.
KB_INDEXES: list[tuple] = [
    ("cc_hdfc", UseCase.credit_card, "hinglish", KB_CC),
    ("col_icici", UseCase.collections, "hinglish", KB_COL),
    ("ins_term", UseCase.insurance, "hi", KB_INS),
]


async def _seed() -> None:
    embedder = build_embedder(get_nlp_settings())

    async with session_scope() as db:
        # Tenant registry + login users (6.4 follow-up). Idempotent — restarts /
        # re-runs never duplicate. Seeds `demo`/`koel-demo` (platform-admin,
        # org_id=None, sees all) so the demo login is unchanged, plus one operator
        # in each of two orgs so login → tenant-scoping is provable end-to-end.
        await seed_orgs(db)
        await seed_users(db)

        # CRM customers
        for record in SEED_CUSTOMERS:
            if await db.get(CrmCustomer, record["id"]) is None:
                db.add(CrmCustomer(**record))

        # Bot instances
        for bot in DEMO_BOTS:
            if await db.get(BotConfig, bot["id"]) is None:
                db.add(BotConfig(**bot))

        # Knowledge base: refresh every index so embeddings always match the current
        # embedder (switching hash → nvidia requires re-embedding). Chunks are
        # "passages". Every configured bot's rag_index is grounded — not just CC.
        for rag_index, use_case, language, chunks in KB_INDEXES:
            await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.rag_index == rag_index))
            for chunk in chunks:
                db.add(
                    KnowledgeChunk(
                        rag_index=rag_index,
                        use_case=use_case,
                        language=language,
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
    print("Bootstrap complete: migrations applied, orgs + users + CRM + bots + knowledge base seeded.")


if __name__ == "__main__":
    _run_migrations()  # sync: owns schema (Alembic) before the async seed
    asyncio.run(_seed())
