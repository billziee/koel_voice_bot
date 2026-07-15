"""SQLAlchemy models — sessions, call logs, transcripts, CRM, RAG knowledge base.

Maps directly onto the PRD §10 action item "Database Schema Design": PostgreSQL
schema for sessions, call logs, transcripts, and CRM records, plus the pgvector
knowledge base for RAG.

PII discipline (PRD §14): customer name + account number are stored on CRM
records (synthetic data only in MVP) but MUST be masked in logs — see
``koel.common.logging``. Transcripts persist post-call for QA/compliance (§06).
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Embedding dimension for the RAG store. 1024 matches Bedrock Titan Embeddings v2.
EMBEDDING_DIM = 1024


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# --- Enums shared with the API layer ----------------------------------------


class CallDirection(str, enum.Enum):
    outgoing = "outgoing"
    incoming = "incoming"


class CallOutcome(str, enum.Enum):
    """Outcome tags written on call end (PRD §05 step 6, §05 resilience table)."""

    converted = "converted"
    declined = "declined"
    escalated = "escalated"
    no_answer = "no_answer"
    customer_unresponsive = "customer_unresponsive"
    call_dropped = "call_dropped"
    unsupported_language = "unsupported_language"
    in_progress = "in_progress"


class EscalationStatus(str, enum.Enum):
    """Lifecycle of a human-handoff request (3.1). The caller's CallLog stays
    ``escalated``; this tracks what the operator did with the handoff."""

    pending = "pending"  # queued, no operator yet
    claimed = "claimed"  # an operator picked it up
    resolved = "resolved"  # operator finished and ended the call
    abandoned = "abandoned"  # caller hung up / grace expired before resolution


class UseCase(str, enum.Enum):
    credit_card = "credit_card"
    collections = "collections"
    personal_loan = "personal_loan"
    insurance = "insurance"
    custom = "custom"


# --- Tenant registry (6.4 Multi-tenant enablement) --------------------------


class Org(Base):
    """Authoritative tenant (organization) registry.

    Org scoping is already pervasive — every table carries a nullable indexed
    ``org_id`` and the read/list routers filter by it (AF). What was missing to
    actually *enable* multi-tenancy is the registry those ids point at. Adding
    it (plus the cross-tenant isolation gate proven in the test suite) is what
    turns the dormant scoping into real multi-tenancy; it is deliberately NOT a
    destructive backfill — single-tenant pilot rows keep ``org_id=NULL``.
    """

    __tablename__ = "orgs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class User(Base):
    """Operator/admin login identity, mapped to an org (6.4 follow-up).

    This closes the last gap in tenant scoping: login (``koel.voicebot.api.auth``)
    resolves the user, verifies their credential, and mints a token carrying this
    user's ``org_id`` + ``role`` — so a logged-in operator is scoped to their
    tenant instead of the unscoped "sees all" principal every prior login minted.

    Two identity kinds share this table (``auth_provider``):
      - ``"password"`` — resolved by ``username``, verified against the bcrypt
        ``password_hash`` (the original demo/CI + seeded-operator path).
      - ``"google"`` — resolved by verified ``email`` (Google Sign-In, allowlist
        model). Such rows carry NO ``password_hash`` and NO ``username``; the
        Google subject id is stored in ``google_sub`` on first login. Only
        pre-provisioned emails exist as rows, so login is an allowlist lookup.

    ``org_id=None`` is the platform-admin / single-tenant "sees all" principal —
    the SAME convention every scoped table's nullable ``org_id`` already uses
    (None = no tenant filter). A tenant-scoped user carries their org's id.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # Nullable: a Google (allowlist) user has no username — they resolve by email.
    username: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    # Nullable: a Google user has no password. bcrypt hash — never plaintext.
    password_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Google Sign-In identity (allowlist). ``email`` is the pre-provisioned key an
    # admin adds; ``google_sub`` is Google's stable subject id, backfilled on first
    # login and matched thereafter (email can change; sub cannot).
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True, nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    auth_provider: Mapped[str] = mapped_column(String(16), default="password")  # password | google
    # Tenant scope. None = platform-admin / single-tenant "sees all" (AF convention).
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(32), default="operator")
    active: Mapped[bool] = mapped_column(default=True)  # soft-disable without deletion
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --- BYOK model connections (custom LLM/TTS/STT) ----------------------------


class ModelConnection(Base):
    """A reusable, org-scoped connection to a customer-supplied model provider
    (BYOK): an OpenAI-compatible or vendor LLM / TTS / STT endpoint. Bots select
    one per role via ``BotConfig.config['connections']``.

    The ``api_key_encrypted`` column stores the provider key encrypted at rest
    (Fernet, see ``koel.common.crypto``) — never plaintext, never returned by the
    API, never sent on the inter-service wire (only the connection id travels;
    each service decrypts server-side to build its provider).
    """

    __tablename__ = "model_connections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # AF; None = shared
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(8))  # "llm" | "tts" | "stt"
    provider: Mapped[str] = mapped_column(String(32), default="openai_compatible")
    base_url: Mapped[str] = mapped_column(String(256), default="")
    api_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    # Role-specific tuning: temperature/top_p/max_tokens (llm), voice/encoding
    # (tts), language/interim (stt), plus persona/preamble. Free-form so a new
    # provider needs no migration.
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --- CRM (Dummy CRM, PRD §06 / §10) -----------------------------------------


class CrmCustomer(Base):
    """Synthetic customer record. Real CRM swaps in behind the same contract."""

    __tablename__ = "crm_customers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # Nullable tenant scope (AF). Single-tenant pilot leaves it NULL; a later
    # backfill assigns orgs without a schema change (multi-tenancy deferred to P2).
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120))  # PII — mask in logs
    phone: Mapped[str] = mapped_column(String(20))
    language: Mapped[str] = mapped_column(String(16), default="hinglish")
    bank: Mapped[str] = mapped_column(String(80), default="HDFC Bank")
    use_case: Mapped[UseCase] = mapped_column(Enum(UseCase), default=UseCase.credit_card)
    # Use-case-specific fields (offer amount, product, DPD, EMI, premium…) kept
    # as JSON so each use case defines its own schema without migrations (§06).
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    dnd: Mapped[bool] = mapped_column(default=False)  # TRAI DND flag (§11)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --- Bot configuration (PRD §06 Configuration Panel) ------------------------


class BotConfig(Base):
    """A configured bot instance. The product's moat: new use cases are config."""

    __tablename__ = "bot_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # AF
    name: Mapped[str] = mapped_column(String(120))
    use_case: Mapped[UseCase] = mapped_column(Enum(UseCase))
    language: Mapped[str] = mapped_column(String(16), default="hinglish")
    voice_gender: Mapped[str] = mapped_column(String(16), default="female")
    speaking_rate: Mapped[float] = mapped_column(Float, default=1.0)
    greeting_template: Mapped[str] = mapped_column(Text, default="")
    rag_enabled: Mapped[bool] = mapped_column(default=True)
    rag_index: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Per-bot RAG tuning overrides (W). NULL → fall back to the NLP_svc defaults.
    rag_top_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rag_score_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Per-bot NLP overrides (roadmap 5.4). NULL → fall back to the NLP_svc
    # defaults. prompt_preamble is ADDED to the hardened system prompt, never a
    # replacement, so the compliance/disclosure/JSON rules always stay in force.
    llm_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    prompt_preamble: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_tone: Mapped[str] = mapped_column(String(16), default="neutral")
    ambient_profile: Mapped[str] = mapped_column(String(16), default="callfloor")
    # Free-form config: CRM field mapping, escalation rules, success criteria.
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --- Calls & transcripts ----------------------------------------------------


class CallLog(Base):
    """One row per call. Written on session end (PRD §05 step 6)."""

    __tablename__ = "call_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # AF
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    bot_id: Mapped[str] = mapped_column(ForeignKey("bot_configs.id"))
    customer_id: Mapped[str | None] = mapped_column(
        ForeignKey("crm_customers.id"), nullable=True
    )
    direction: Mapped[CallDirection] = mapped_column(
        Enum(CallDirection), default=CallDirection.outgoing
    )
    language: Mapped[str] = mapped_column(String(16), default="hinglish")
    outcome: Mapped[CallOutcome] = mapped_column(
        Enum(CallOutcome), default=CallOutcome.in_progress
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    escalated: Mapped[bool] = mapped_column(default=False)
    recording_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Latency rollups + consent log + trace id for the §14 NFR dashboards.
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)

    transcript_turns: Mapped[list[TranscriptTurn]] = relationship(
        back_populates="call", cascade="all, delete-orphan"
    )


class TranscriptTurn(Base):
    """A single conversational turn. Persisted for QA, compliance, replay (§06)."""

    __tablename__ = "transcript_turns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(ForeignKey("call_logs.id"), index=True)
    turn_index: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(16))  # "bot" | "customer"
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    interrupted: Mapped[bool] = mapped_column(default=False)  # barge-in (§05)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    call: Mapped[CallLog] = relationship(back_populates="transcript_turns")


class EscalationRequest(Base):
    """A human-handoff request (3.1). Created when the bot escalates; carries the
    full context an operator needs (transcript + CRM) so the handoff is 100%
    (PRD §02). The caller's socket holds in ``waiting_agent`` until an operator
    claims + resolves it (or the caller drops → abandoned)."""

    __tablename__ = "escalation_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # AF tenant scope
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    bot_id: Mapped[str] = mapped_column(String(64))
    customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(String(128), default="")
    language: Mapped[str] = mapped_column(String(16), default="hinglish")
    status: Mapped[EscalationStatus] = mapped_column(
        Enum(EscalationStatus), default=EscalationStatus.pending, index=True
    )
    operator_sub: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Full handoff context so the operator needs nothing else (PRD §02).
    transcript: Mapped[list] = mapped_column(JSON, default=list)
    crm_context: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExportJob(Base):
    """Audit trail of a regulatory export (6.5). The export itself is a JSONL
    bundle + SHA-256 manifest; this row records who exported which range, how
    many records, the manifest hash, and where the bundle landed — the export
    action is itself audit-relevant (PRD §10 / §11)."""

    __tablename__ = "export_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    range_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    range_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    manifest_sha256: Mapped[str] = mapped_column(String(64), default="")
    object_url: Mapped[str | None] = mapped_column(String(256), nullable=True)  # None ⇒ returned inline
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ConsentRecord(Base):
    """Evidentiary verbal-consent record (2.3, PRD §04 insurance / §11 IRDAI).

    Links the exact transcript turn where the customer affirmed consent to the
    call recording + the disclosure version read to them, so the consent is
    verifiable after the fact. Written at call end only for consent-required bots."""

    __tablename__ = "consent_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    call_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    disclosure_version: Mapped[str] = mapped_column(String(64), default="")
    turn_index: Mapped[int] = mapped_column(Integer, default=-1)  # transcript turn that affirmed
    consent_text: Mapped[str] = mapped_column(Text, default="")  # the customer's affirmative words
    recording_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DialAttemptStatus(str, enum.Enum):
    """Lifecycle of one outbound dial (1.2)."""

    pending = "pending"  # due at scheduled_at, not yet claimed
    in_progress = "in_progress"  # a worker claimed it and is dialing
    done = "done"  # connected + completed (terminal)
    failed = "failed"  # exhausted retries / non-retryable outcome (terminal)


class Campaign(Base):
    """An outbound campaign (1.2): a bot dialing a segment within a window, with a
    retry policy. The dialer worker originates ``DialAttempt`` rows for it."""

    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    bot_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), default="")
    active: Mapped[bool] = mapped_column(default=True)
    # {max_attempts, backoff_seconds, retry_outcomes: [...]} — see pipeline/dialer.py.
    retry_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DialAttempt(Base):
    """One scheduled outbound dial (1.2) — also backs scheduled callbacks (2.5).
    A worker claims due rows (``pending`` + ``scheduled_at`` <= now) with
    FOR UPDATE SKIP LOCKED so multiple workers don't double-dial."""

    __tablename__ = "dial_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[DialAttemptStatus] = mapped_column(
        Enum(DialAttemptStatus), default=DialAttemptStatus.pending, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    last_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --- RAG knowledge base (pgvector) ------------------------------------------


class KnowledgeChunk(Base):
    """Product knowledge, disclosures, objection-handling scripts (PRD §06 RAG)."""

    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    org_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # AF
    rag_index: Mapped[str] = mapped_column(String(64), index=True)
    use_case: Mapped[UseCase] = mapped_column(Enum(UseCase))
    language: Mapped[str] = mapped_column(String(16), default="en")
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --- CRM interaction log + config audit (AD / AF) ---------------------------


class InteractionLogRow(Base):
    """Persisted CRM interaction (AD). idempotency_key makes retried writes safe."""

    __tablename__ = "interaction_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    event: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    # Orchestrator retries send the same key → unique constraint dedupes the write.
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BotConfigAudit(Base):
    """Who changed a bot config, when, and the before/after (AF governance)."""

    __tablename__ = "bot_config_audits"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    bot_id: Mapped[str] = mapped_column(String(32), index=True)
    user_sub: Mapped[str] = mapped_column(String(120))
    op: Mapped[str] = mapped_column(String(16))  # "create" | "update"
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
