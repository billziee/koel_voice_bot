"""Seeded login identities for demo / CI (6.4 follow-up).

Parallel to ``koel.crm.seed`` (synthetic CRM data): fabricated login users so the
dashboard demo login works and the cross-tenant isolation suite can log in as
real tenants. No real credentials — every password here is a throwaway showcase
value (acceptable for demos per PRD §08).

Seeded set:
  - ``demo`` / ``koel-demo`` — platform-admin (``org_id=None``, role ``admin``):
    the existing demo login, which still sees ALL tenants (the unscoped
    "sees all" principal every scoped router honours).
  - ``orga_operator`` — tenant operator scoped to org ``orgA``.
  - ``orgb_operator`` — tenant operator scoped to org ``orgB``.

The two org-scoped operators let login → org-scoping be proven end-to-end: a
token minted by the REAL ``/v1/auth/login`` for ``orga_operator`` sees only org
A's rows. The matching ``orgs`` registry rows are seeded too so the ids the
operators carry point at real tenants.

Seeding is idempotent — it inserts only what's missing, so restarts / re-runs
never duplicate. bcrypt hashes are computed ONCE at import (bcrypt is slow), not
per call, so repeated seeding in the test suite stays cheap.
"""

from __future__ import annotations

from dataclasses import dataclass

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from koel.db.models import Org, User

# Org ids the seeded operators are scoped to. Deliberately distinct from the
# ad-hoc ids the isolation suite mints inline ("org_a"/"globex") so a real
# login-derived token and a hand-minted token never collide.
ORG_A_ID = "orgA"
ORG_B_ID = "orgB"


def _hash(password: str) -> bytes:
    """bcrypt hash (matches ``koel.voicebot.api.auth._verify``)."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt())


@dataclass(frozen=True)
class _UserSpec:
    username: str
    password: str
    org_id: str | None
    role: str


# username → (plaintext password, org scope, role). Plaintext only lives here for
# the demo/CI login path; the stored column is the bcrypt hash, never the text.
_USER_SPECS: tuple[_UserSpec, ...] = (
    _UserSpec("demo", "koel-demo", None, "admin"),
    _UserSpec("orga_operator", "orga-demo", ORG_A_ID, "operator"),
    _UserSpec("orgb_operator", "orgb-demo", ORG_B_ID, "operator"),
)

# (id, name, slug) for the tenant registry rows backing the scoped operators.
_ORG_SPECS: tuple[tuple[str, str, str], ...] = (
    (ORG_A_ID, "Org A (demo tenant)", "org-a"),
    (ORG_B_ID, "Org B (demo tenant)", "org-b"),
)

# Precompute hashes ONCE at import — bcrypt is intentionally slow, so hashing per
# seed call would tax every test that builds a fresh in-memory DB.
_SEEDED: tuple[tuple[str, bytes, str | None, str], ...] = tuple(
    (spec.username, _hash(spec.password), spec.org_id, spec.role) for spec in _USER_SPECS
)


async def seed_users(session: AsyncSession) -> int:
    """Insert any missing seed users into ``session``; return how many were added.

    Idempotent: an existing username is left untouched, so re-running (startup,
    test setup, ``bootstrap_db``) never duplicates. The caller owns the commit
    (matches ``bootstrap_db``'s ``session_scope`` pattern).
    """
    inserted = 0
    for username, password_hash, org_id, role in _SEEDED:
        existing = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                User(username=username, password_hash=password_hash, org_id=org_id, role=role)
            )
            inserted += 1
    return inserted


async def seed_orgs(session: AsyncSession) -> int:
    """Insert the demo tenant registry rows the scoped operators point at.

    Idempotent (keyed by id). Caller owns the commit.
    """
    inserted = 0
    for org_id, name, slug in _ORG_SPECS:
        if await session.get(Org, org_id) is None:
            session.add(Org(id=org_id, name=name, slug=slug))
            inserted += 1
    return inserted


# Roles a Google-provisioned HUMAN may hold. Deliberately excludes "service" (the
# internal machine principal minted only by koel.voicebot.clients for M2M calls):
# a config typo like `alice@acme.com::service` must NEVER mint a service-principal
# token for a human dashboard login. Anything unrecognized is coerced to the
# least-privilege "operator" rather than granting an unintended scope.
_GOOGLE_HUMAN_ROLES: frozenset[str] = frozenset({"operator", "admin"})


def parse_google_allowlist(raw: str) -> tuple[tuple[str, str | None, str], ...]:
    """Parse the ``KOEL_GOOGLE_ALLOWLIST`` CSV into ``(email, org_id, role)`` rows.

    Each entry is ``email[:org_id[:role]]``. A blank org_id ⇒ ``None`` (the
    platform-admin "sees all" principal); a missing role ⇒ ``"operator"``. Emails
    are lowercased (Google treats them case-insensitively; we store + match the
    normalized form). ``role`` is lowercased and validated against
    ``_GOOGLE_HUMAN_ROLES`` — an unrecognized or privileged value (e.g. "service")
    is coerced to "operator" so a config mistake can't escalate privilege.
    Malformed entries (no ``@``) are skipped rather than raising, so one bad line
    can't wedge boot.
    """
    entries: list[tuple[str, str | None, str]] = []
    for raw_entry in raw.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split(":")]
        email = parts[0].lower()
        if "@" not in email:
            continue
        org_id = parts[1] if len(parts) > 1 and parts[1] else None
        role = (parts[2].lower() if len(parts) > 2 and parts[2] else "operator")
        if role not in _GOOGLE_HUMAN_ROLES:
            role = "operator"
        entries.append((email, org_id, role))
    return tuple(entries)


async def seed_google_allowlist(session: AsyncSession, raw: str) -> int:
    """Reconcile the Google Sign-In allowlist users to the config CSV.

    Returns how many rows were newly inserted. Idempotent + config-synced: the env
    is the SOLE source of truth for who may sign in with Google, so:
      - a new allowlisted email is inserted as a PASSWORDLESS row
        (``auth_provider="google"``, no username, no password_hash),
      - an existing allowlisted email has its ``org_id`` + ``role`` re-synced and
        is re-activated (``google_sub``, bound on first login, is left untouched),
      - a Google row whose email is NO LONGER in the CSV is DEACTIVATED
        (``active=False``) — so removing an email from the env and redeploying is
        an actual revocation, not a silent no-op. Deactivation (not deletion)
        keeps the audit trail + sub binding, and re-adding the email re-activates.

    Caller owns the commit. A blank ``raw`` deactivates every Google row (an empty
    allowlist ⇒ nobody may sign in with Google).
    """
    entries = parse_google_allowlist(raw)
    allowed_emails = {email for email, _org, _role in entries}
    inserted = 0
    for email, org_id, role in entries:
        existing = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                User(
                    email=email,
                    username=None,
                    password_hash=None,
                    auth_provider="google",
                    org_id=org_id,
                    role=role,
                    active=True,
                )
            )
            inserted += 1
        else:
            existing.org_id = org_id
            existing.role = role
            existing.active = True
            existing.auth_provider = "google"
    # Revoke: deactivate any still-active Google row that dropped off the list.
    stale = (
        await session.execute(
            select(User).where(User.auth_provider == "google", User.active.is_(True))
        )
    ).scalars().all()
    for user in stale:
        if user.email not in allowed_emails:
            user.active = False
    return inserted
