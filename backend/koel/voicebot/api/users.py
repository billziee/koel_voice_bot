"""Operator/admin account provisioning API (org + user admin).

Companion to ``koel.voicebot.api.orgs``: that module registers tenants, this
one provisions the *people* inside them. An admin invites a teammate by
username + email; the invitee sets their own password via a one-time link
(``accept-invite``) instead of the admin ever handling a plaintext password.
The SAME token mechanism (``koel.common.tokens``) also drives an admin-
triggered password reset — the row's existing password stays valid until the
new one is set, so a reset never locks someone out mid-flow.

Admin-vs-tenant gating (mirrors ``orgs.py``, one level less strict): any
``role == "admin"`` principal may manage accounts. A **platform** admin
(``org_id is None``) may act on any org, including provisioning another
platform admin. A **tenant-scoped** admin (``org_id`` set) may only see/act on
users within their OWN org and can never create/target a platform-admin row or
another tenant's row — enforced at each mutating route, not just the list view.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import bcrypt
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from koel.common.auth import CurrentUser, TokenClaims, issue_token
from koel.common.config import get_common_settings
from koel.common.errors import ApiError
from koel.common.logging import get_logger, mask_account
from koel.common.messaging import Channel, send_message
from koel.common.ratelimit import rate_limit
from koel.common.tokens import generate_token, hash_verifier, split_token, verify_verifier
from koel.db.models import User
from koel.db.session import session_scope

logger = get_logger("koel.voicebot.users")

Role = Literal["operator", "admin"]
# Basic shape check only (no email-validator dependency, matching the rest of the
# codebase — Google login's email claim is likewise plain `str`, trusted because
# it comes pre-verified from Google's JWKS, not from Pydantic).
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
# Constant-work decoy for the "no such selector" branch — mirrors auth.py's
# _DECOY_HASH so an unknown selector spends roughly the same wall-clock time
# as a known-selector/wrong-verifier case (both run one hash + compare_digest),
# rather than short-circuiting on the DB miss alone.
_DECOY_VERIFIER_HASH = hash_verifier("koel-unknown-selector-timing-guard")

# Public, pre-auth, brute-force sensitive (a guessed token sets someone's
# password) — fails CLOSED like login, in its own rate-limit namespace.
_accept_limit = rate_limit(max_requests=10, window_s=60, name="accept_invite", fail_open=False)
# Post-auth defense-in-depth: reset-password/resend-invite each trigger an
# outbound email once the channel is configured, and had NO cooldown at all —
# an admin token (even a low-trust tenant-scoped one) could otherwise be used
# to email-bomb a target. Generous cap so legitimate bulk admin work isn't
# blocked; fail-open like other authenticated routes (an outage here degrades
# to "no throttle", not "admin locked out").
_credential_action_limit = rate_limit(max_requests=20, window_s=60, name="user_credential_action")


async def require_user_admin(user: CurrentUser) -> TokenClaims:
    """Only ``role == "admin"`` may manage accounts — an operator or the
    service principal must never provision or reset another login's credential."""
    if user.role != "admin":
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="forbidden",
            message="Managing user accounts requires an admin role.",
        )
    return user


UserAdmin = Annotated[TokenClaims, Depends(require_user_admin)]

router = APIRouter(prefix="/v1/users", tags=["users"])


def _assert_org_reachable(admin: TokenClaims, target_org_id: str | None) -> None:
    """A tenant-scoped admin (``org_id`` set) may only reach their OWN org — never
    the platform-admin scope (``org_id=None``) nor a different tenant's. A
    platform admin (``org_id is None``) reaches everything."""
    if admin.org_id is not None and target_org_id != admin.org_id:
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="forbidden",
            message="You may only manage accounts within your own organization.",
        )


def _hash_password(password: str) -> bytes:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt())


def _aware(dt: datetime) -> datetime:
    """SQLite (tests) hands back a naive datetime even for a DateTime(timezone=True)
    column; treat naive as UTC (it was always written via datetime.now(UTC)) rather
    than crash on a naive/aware compare — same convention as orchestrator.py's
    ISO-timestamp parsing."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class UserOut(BaseModel):
    id: str
    username: str | None
    email: str | None
    org_id: str | None
    role: str
    active: bool
    invite_pending: bool  # password_hash is NULL — provisioned but never accepted
    created_at: str | None = None


def _to_out(row: User) -> UserOut:
    return UserOut(
        id=row.id,
        username=row.username,
        email=row.email,
        org_id=row.org_id,
        role=row.role,
        active=row.active,
        invite_pending=row.password_hash is None,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


def _invite_url(combined_token: str) -> str:
    base = get_common_settings().invite_base_url.rstrip("/")
    return f"{base}/?invite={combined_token}" if base else f"/?invite={combined_token}"


async def _issue_credential_token(user: User, *, admin_sub: str) -> str:
    """Mint a fresh invite/reset token for ``user`` (mutates the passed row —
    caller commits) and return the combined token to embed in the link."""
    selector, verifier, combined = generate_token()
    user.invite_selector = selector
    user.invite_verifier_hash = hash_verifier(verifier)
    user.invite_expires_at = datetime.now(UTC) + timedelta(hours=get_common_settings().invite_ttl_hours)
    user.invited_by = admin_sub
    return combined


async def _send_credential_email(*, to: str | None, url: str, is_reset: bool) -> bool:
    """Best-effort — never raises. Returns whether the send actually succeeded
    (not merely whether a gateway is configured) so the caller can decide
    whether it's still safe to echo the raw link back in the API response."""
    if not to:
        return False
    verb = "Reset your KOEL password" if is_reset else "Set up your KOEL account"
    body = f"{verb}: {url}\n\nThis link expires in {get_common_settings().invite_ttl_hours:.0f} hours."
    return await send_message(Channel.email, to, body)


# --- list / read -------------------------------------------------------------


@router.get("", response_model=list[UserOut])
async def list_users(admin: UserAdmin, org_id: str | None = Query(default=None)) -> list[UserOut]:
    """Accounts the caller may manage: a tenant-scoped admin always sees only
    their own org (the ``org_id`` query param is ignored for them — it exists
    for a platform admin to filter to one tenant); a platform admin sees all,
    or one org when ``org_id`` is given."""
    effective_org_id = admin.org_id if admin.org_id is not None else org_id
    stmt = select(User).order_by(User.created_at.asc())
    if effective_org_id is not None:
        stmt = stmt.where(User.org_id == effective_org_id)
    async with session_scope() as db:
        rows = (await db.execute(stmt)).scalars().all()
    return [_to_out(r) for r in rows]


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: str, admin: UserAdmin) -> UserOut:
    async with session_scope() as db:
        row = await db.get(User, user_id)
    if row is None:
        raise ApiError(
            status_code=status.HTTP_404_NOT_FOUND, code="user_not_found", message="No such user."
        )
    _assert_org_reachable(admin, row.org_id)
    return _to_out(row)


# --- invite / accept ---------------------------------------------------------


class InviteIn(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL_PATTERN)
    role: Role = "operator"
    # Ignored (forced to the caller's own org) for a tenant-scoped admin; a
    # platform admin may target any org, or omit it for another platform admin.
    org_id: str | None = None


class InviteOut(BaseModel):
    user: UserOut
    # Only present when the email channel is dormant/failed — once a real send
    # SUCCEEDS, the raw (immediately-usable) link is no longer echoed back into
    # the API response, narrowing where this bearer-equivalent secret transits
    # (proxies/APM/log capture) to just the genuine fallback case.
    invite_url: str | None
    email_sent: bool


@router.post("/invite", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
async def invite_user(payload: InviteIn, admin: UserAdmin) -> InviteOut:
    """Provision a pending account and return a one-time accept-invite link.
    409 on a duplicate username/email — both are unique identities."""
    target_org_id = admin.org_id if admin.org_id is not None else payload.org_id
    _assert_org_reachable(admin, target_org_id)

    async with session_scope() as db:
        existing_stmt = select(User).where(
            (User.username == payload.username) | (User.email == payload.email)
        )
        if admin.org_id is not None:
            # Tenant-scoped admin: check existence ONLY within their own org, so
            # the app-level query itself can't be used for broader cross-tenant
            # reconnaissance, and any 409 it raises never differs in code/detail
            # from the IntegrityError-fallback case below.
            #
            # RESIDUAL LIMITATION (not fully closed by this — tracked as a
            # follow-up): username/email are GLOBALLY unique at the DB level
            # (`users.username`/`users.email`, from the original single-tenant
            # schema), so a guessed identity that collides with ANOTHER org's
            # user still surfaces as 409 (via the IntegrityError catch below)
            # while a non-existent one returns 201 — that binary "exists
            # somewhere on the platform" bit is inherent to global uniqueness
            # and isn't removed by scoping this proactive check alone. Actually
            # closing it needs per-org unique constraints (composite on
            # org_id+username, org_id+email) plus a matching change to how
            # /v1/auth/login resolves a bare username without an org hint —
            # out of scope for this change, tracked separately.
            existing_stmt = existing_stmt.where(User.org_id == admin.org_id)
        existing = (await db.execute(existing_stmt)).scalars().first()
        if existing is not None:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code="user_exists",
                message="A user with that username or email already exists.",
            )
        row = User(
            username=payload.username,
            email=payload.email,
            org_id=target_org_id,
            role=payload.role,
            auth_provider="password",
        )
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            # The org-scoped check above couldn't see a collision outside the
            # caller's org; the DB's global unique constraint just caught one.
            # Same code/message as the org-scoped case — never distinguish them.
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code="user_exists",
                message="A user with that username or email already exists.",
            ) from None
        combined = await _issue_credential_token(row, admin_sub=admin.sub)
        await db.flush()
        out = _to_out(row)
    url = _invite_url(combined)
    sent = await _send_credential_email(to=payload.email, url=url, is_reset=False)
    logger.info(
        "user_invited",
        extra={"extra_fields": {
            "user_id": out.id, "org_id": target_org_id, "by": admin.sub,
        }},
    )
    return InviteOut(user=out, invite_url=None if sent else url, email_sent=sent)


class AcceptInviteIn(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=8, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post(
    "/accept-invite", response_model=TokenResponse, dependencies=[Depends(_accept_limit)]
)
async def accept_invite(payload: AcceptInviteIn) -> TokenResponse:
    """Public: redeem an invite/reset token and set a password. Works whether
    the account was pending (invite) or already active (reset) — either way the
    presented token must match an UNEXPIRED selector+verifier pair. Returns an
    access token so an invite-acceptor lands logged in without a second step."""
    parts = split_token(payload.token)
    if parts is None:
        raise ApiError(
            status_code=status.HTTP_400_BAD_REQUEST, code="malformed_token", message="Invalid token."
        )
    selector, verifier = parts
    async with session_scope() as db:
        row = (
            await db.execute(select(User).where(User.invite_selector == selector))
        ).scalar_one_or_none()
        # Compare against the DECOY hash when there's no real one to check
        # (unknown selector, or a row with no token in flight) so this branch
        # spends the same wall-clock time (one hash + compare_digest) as a
        # known-selector/wrong-verifier case — mirrors auth.py's _DECOY_HASH.
        verifier_hash = row.invite_verifier_hash if row is not None else None
        verifier_ok = verify_verifier(verifier, verifier_hash or _DECOY_VERIFIER_HASH)
        # Identical rejection for "no such selector", "verifier mismatch",
        # "expired", AND "deactivated" — never tell an attacker which half of
        # the check failed. The `not row.active` arm is load-bearing: without
        # it, redeeming a still-outstanding token would silently reverse an
        # admin's explicit deactivate_user call from this UNAUTHENTICATED route
        # (deactivate_user also revokes the token below, but this is the
        # backstop for any token issued/still outstanding around that edge).
        invalid = (
            row is None
            or not row.active
            or verifier_hash is None
            or not verifier_ok
            or row.invite_expires_at is None
            or _aware(row.invite_expires_at) < datetime.now(UTC)
        )
        if invalid:
            raise ApiError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="invalid_or_expired_token",
                message="This link is invalid or has expired. Ask an admin to send a new one.",
            )
        assert row is not None  # noqa: S101 — narrowed by `invalid` above, for mypy
        row.password_hash = _hash_password(payload.password)
        row.invite_selector = None
        row.invite_verifier_hash = None
        row.invite_expires_at = None
        row.invited_by = None
        # NOT `row.active = True`: a fresh invite's row is already active=True by
        # construction (invite_user never sets it false), so this would only ever
        # fire for a deactivated row — silently undoing deactivate_user. Explicit
        # reactivation stays an authenticated admin action (POST .../reactivate).
        username, role, org_id = row.username, row.role, row.org_id
    logger.info("user_credential_set", extra={"extra_fields": {"username": mask_account(username or "")}})
    # A password identity always has a username by construction (invite_user
    # requires it) — the subject a normal password login would mint against.
    return TokenResponse(access_token=issue_token(username or "", kind="access", role=role, org_id=org_id))


# --- manage -------------------------------------------------------------------


class UserPatch(BaseModel):
    role: Role | None = None
    active: bool | None = None


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: str, payload: UserPatch, admin: UserAdmin) -> UserOut:
    async with session_scope() as db:
        row = await db.get(User, user_id)
        if row is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND, code="user_not_found", message="No such user."
            )
        _assert_org_reachable(admin, row.org_id)
        before_role, before_active = row.role, row.active
        if payload.role is not None:
            row.role = payload.role
        if payload.active is not None:
            row.active = payload.active
        await db.flush()
        out = _to_out(row)
    logger.info(
        "user_updated",
        extra={"extra_fields": {
            "user_id": user_id, "by": admin.sub,
            # This is the one route where a non-platform principal can mint a
            # new admin (role escalation within their own org, by design) —
            # record the before/after diff so that event is auditable, not
            # indistinguishable from an unrelated no-op patch in the log stream.
            "role": f"{before_role}->{out.role}" if before_role != out.role else out.role,
            "active": f"{before_active}->{out.active}" if before_active != out.active else out.active,
        }},
    )
    return out


@router.post("/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(user_id: str, admin: UserAdmin) -> UserOut:
    async with session_scope() as db:
        row = await db.get(User, user_id)
        if row is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND, code="user_not_found", message="No such user."
            )
        _assert_org_reachable(admin, row.org_id)
        row.active = False
        # Revoke any outstanding invite/reset token — otherwise a still-valid
        # link (issued before this deactivation, up to invite_ttl_hours old)
        # would remain redeemable via the public accept-invite route. Belt and
        # suspenders alongside accept_invite's own `not row.active` check.
        row.invite_selector = None
        row.invite_verifier_hash = None
        row.invite_expires_at = None
        await db.flush()
        out = _to_out(row)
    logger.info("user_deactivated", extra={"extra_fields": {"user_id": user_id, "by": admin.sub}})
    return out


@router.post("/{user_id}/reactivate", response_model=UserOut)
async def reactivate_user(user_id: str, admin: UserAdmin) -> UserOut:
    async with session_scope() as db:
        row = await db.get(User, user_id)
        if row is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND, code="user_not_found", message="No such user."
            )
        _assert_org_reachable(admin, row.org_id)
        row.active = True
        await db.flush()
        out = _to_out(row)
    logger.info("user_reactivated", extra={"extra_fields": {"user_id": user_id, "by": admin.sub}})
    return out


@router.post(
    "/{user_id}/reset-password", response_model=InviteOut, dependencies=[Depends(_credential_action_limit)]
)
async def reset_password(user_id: str, admin: UserAdmin) -> InviteOut:
    """Generate a fresh credential-set link WITHOUT touching the current
    password — the old one keeps working until this one is redeemed, so a
    reset in flight never locks the user out."""
    async with session_scope() as db:
        row = await db.get(User, user_id)
        if row is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND, code="user_not_found", message="No such user."
            )
        _assert_org_reachable(admin, row.org_id)
        combined = await _issue_credential_token(row, admin_sub=admin.sub)
        await db.flush()
        out = _to_out(row)
        email = row.email
    url = _invite_url(combined)
    sent = await _send_credential_email(to=email, url=url, is_reset=True)
    logger.info("user_password_reset_issued", extra={"extra_fields": {"user_id": user_id, "by": admin.sub}})
    return InviteOut(user=out, invite_url=None if sent else url, email_sent=sent)


@router.post(
    "/{user_id}/resend-invite", response_model=InviteOut, dependencies=[Depends(_credential_action_limit)]
)
async def resend_invite(user_id: str, admin: UserAdmin) -> InviteOut:
    """Regenerate the invite link for a STILL-PENDING account (password never
    set). 409 if the invite was already accepted — use reset-password instead."""
    async with session_scope() as db:
        row = await db.get(User, user_id)
        if row is None:
            raise ApiError(
                status_code=status.HTTP_404_NOT_FOUND, code="user_not_found", message="No such user."
            )
        _assert_org_reachable(admin, row.org_id)
        if row.password_hash is not None:
            raise ApiError(
                status_code=status.HTTP_409_CONFLICT,
                code="already_accepted",
                message="This account already has a password. Use reset-password instead.",
            )
        combined = await _issue_credential_token(row, admin_sub=admin.sub)
        await db.flush()
        out = _to_out(row)
        email = row.email
    url = _invite_url(combined)
    sent = await _send_credential_email(to=email, url=url, is_reset=False)
    logger.info("user_invite_resent", extra={"extra_fields": {"user_id": user_id, "by": admin.sub}})
    return InviteOut(user=out, invite_url=None if sent else url, email_sent=sent)
