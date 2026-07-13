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
