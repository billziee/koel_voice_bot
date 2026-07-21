"""Account provisioning API: invite / accept-invite / manage (org + user admin).

Covers: platform-admin vs tenant-scoped-admin gating (mirrors test_orgs_api.py),
the invite→accept-invite happy path (returns a working access token), token
validation (malformed / wrong / expired / already-redeemed), admin-triggered
password reset (old password stays valid until redeemed), resend-invite's
already-accepted guard, and org-scoped list/patch/deactivate/reactivate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from koel.common.auth import decode_token, issue_token
from koel.db.models import User
from koel.db.session import session_scope


@pytest.fixture(autouse=True)
def _disable_shared_rate_limits(app_client):
    """accept-invite's (and login's, exercised by the reset-password test)
    limiters are shared module-level closures across the WHOLE test session —
    see test_auth_login.py's _disable_login_limit — so other test files calling
    the same routes can leave either bucket already saturated. Cleared
    automatically by app_client's dependency_overrides.clear()."""
    from koel.voicebot.api import auth as auth_mod
    from koel.voicebot.api import users as users_mod
    from koel.voicebot.main import app

    app.dependency_overrides[users_mod._accept_limit] = lambda: None
    app.dependency_overrides[auth_mod._login_limit] = lambda: None


def _admin_headers(org_id: str | None = None, sub: str = "admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(sub, kind='access', role='admin', org_id=org_id)}"}


def _operator_headers(org_id: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('op', kind='access', role='operator', org_id=org_id)}"}


async def test_platform_admin_invite_accept_happy_path(app_client):
    client, _ = app_client
    r = await client.post(
        "/v1/users/invite",
        headers=_admin_headers(org_id=None),
        json={"username": "newop", "email": "newop@acme.com", "role": "operator", "org_id": "acme"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["invite_pending"] is True
    assert body["user"]["org_id"] == "acme"
    assert "?invite=" in body["invite_url"]
    assert body["email_sent"] is False  # dormant channel (no gateway configured in tests)

    token = body["invite_url"].split("?invite=")[1]
    accept = await client.post(
        "/v1/users/accept-invite", json={"token": token, "password": "correct horse battery staple"}
    )
    assert accept.status_code == 200, accept.text
    access_token = accept.json()["access_token"]
    claims = decode_token(access_token, expected_kind="access")
    assert claims.sub == "newop" and claims.role == "operator" and claims.org_id == "acme"

    # Now shows as accepted (no longer pending), and the SAME token can't be reused.
    got = await client.get(f"/v1/users/{body['user']['id']}", headers=_admin_headers(org_id=None))
    assert got.json()["invite_pending"] is False
    replay = await client.post(
        "/v1/users/accept-invite", json={"token": token, "password": "another password entirely"}
    )
    assert replay.status_code == 400 and replay.json()["code"] == "invalid_or_expired_token"


async def test_org_scoped_admin_invite_forces_own_org(app_client):
    client, _ = app_client
    # payload asks for org "other" but the caller is scoped to "acme" — forced.
    r = await client.post(
        "/v1/users/invite",
        headers=_admin_headers(org_id="acme"),
        json={"username": "scopedop", "email": "scoped@acme.com", "org_id": "other"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["user"]["org_id"] == "acme"


async def test_org_scoped_admin_cannot_reach_another_org(app_client, test_db):
    client, _ = app_client
    # Seed a user directly in a DIFFERENT org.
    async with session_scope() as db:
        db.add(User(id="u_other", username="otherguy", org_id="orgB", role="operator"))
        await db.flush()

    acme_admin = _admin_headers(org_id="orgA")
    assert (await client.get("/v1/users/u_other", headers=acme_admin)).status_code == 403
    assert (
        await client.patch("/v1/users/u_other", headers=acme_admin, json={"active": False})
    ).status_code == 403
    assert (await client.post("/v1/users/u_other/deactivate", headers=acme_admin)).status_code == 403
    assert (await client.post("/v1/users/u_other/reactivate", headers=acme_admin)).status_code == 403
    assert (await client.post("/v1/users/u_other/reset-password", headers=acme_admin)).status_code == 403
    assert (await client.post("/v1/users/u_other/resend-invite", headers=acme_admin)).status_code == 403


async def test_list_users_is_org_scoped(app_client, test_db):
    client, _ = app_client
    async with session_scope() as db:
        db.add(User(id="u_a1", username="a1", org_id="orgA", role="operator"))
        db.add(User(id="u_b1", username="b1", org_id="orgB", role="operator"))
        await db.flush()

    # Tenant-scoped admin sees only their own org, regardless of the org_id query param.
    r = await client.get("/v1/users?org_id=orgB", headers=_admin_headers(org_id="orgA"))
    ids = {u["id"] for u in r.json()}
    assert "u_a1" in ids and "u_b1" not in ids

    # Platform admin with no filter sees both.
    r = await client.get("/v1/users", headers=_admin_headers(org_id=None))
    ids = {u["id"] for u in r.json()}
    assert {"u_a1", "u_b1"}.issubset(ids)

    # Platform admin filtered to one org sees only that org.
    r = await client.get("/v1/users?org_id=orgB", headers=_admin_headers(org_id=None))
    ids = {u["id"] for u in r.json()}
    assert "u_b1" in ids and "u_a1" not in ids


async def test_duplicate_username_or_email_is_409(app_client):
    client, _ = app_client
    admin = _admin_headers(org_id=None)
    first = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "dupe", "email": "dupe@x.com"}
    )
    assert first.status_code == 201

    dup_username = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "dupe", "email": "different@x.com"}
    )
    assert dup_username.status_code == 409 and dup_username.json()["code"] == "user_exists"

    dup_email = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "different", "email": "dupe@x.com"}
    )
    assert dup_email.status_code == 409


async def test_reset_password_keeps_old_password_valid_until_redeemed(app_client, test_db):
    client, _ = app_client
    admin = _admin_headers(org_id=None)
    invite = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "resetme", "email": "resetme@x.com"}
    )
    user_id = invite.json()["user"]["id"]
    old_token = invite.json()["invite_url"].split("?invite=")[1]
    await client.post(
        "/v1/users/accept-invite", json={"token": old_token, "password": "the original password"}
    )

    reset = await client.post(f"/v1/users/{user_id}/reset-password", headers=admin)
    assert reset.status_code == 200, reset.text
    assert reset.json()["user"]["invite_pending"] is False  # password still set, not wiped

    # Old password still works via the normal login route (reset doesn't clear it).
    login = await client.post(
        "/v1/auth/login", json={"username": "resetme", "password": "the original password"}
    )
    assert login.status_code == 200, login.text

    # Redeeming the reset link sets a NEW password.
    new_token = reset.json()["invite_url"].split("?invite=")[1]
    accepted = await client.post(
        "/v1/users/accept-invite", json={"token": new_token, "password": "a brand new password"}
    )
    assert accepted.status_code == 200, accepted.text

    # Old password now rejected; new one works.
    old_login = await client.post(
        "/v1/auth/login", json={"username": "resetme", "password": "the original password"}
    )
    assert old_login.status_code == 401
    new_login = await client.post(
        "/v1/auth/login", json={"username": "resetme", "password": "a brand new password"}
    )
    assert new_login.status_code == 200


async def test_reset_password_with_no_email_on_file_skips_the_send_but_still_returns_a_link(
    app_client, test_db
):
    # `_send_credential_email` short-circuits to `False` (never even reaches the
    # channel) when the row has no email on file — an admin-created/imported
    # account with no address yet. The caller must still get back a usable,
    # immediately-redeemable link since nothing was (or could be) emailed.
    client, _ = app_client
    async with session_scope() as db:
        db.add(
            User(
                id="u_noemail",
                username="noemailuser",
                email=None,
                org_id="acme",
                role="operator",
                active=True,
                password_hash=b"irrelevant-hash",
            )
        )
        await db.flush()

    admin = _admin_headers(org_id="acme")
    reset = await client.post("/v1/users/u_noemail/reset-password", headers=admin)
    assert reset.status_code == 200, reset.text
    assert reset.json()["email_sent"] is False
    assert reset.json()["invite_url"] is not None and "?invite=" in reset.json()["invite_url"]


async def test_patch_role_only_leaves_active_untouched(app_client, test_db):
    # Partial patches must not clobber the field the caller didn't mention.
    async with session_scope() as db:
        db.add(User(id="u_role_only", username="roleonly", org_id="acme", role="operator", active=True))
        await db.flush()

    client, _ = app_client
    admin = _admin_headers(org_id="acme")
    r = await client.patch("/v1/users/u_role_only", headers=admin, json={"role": "admin"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"
    assert r.json()["active"] is True  # unchanged — active was never in the payload


async def test_patch_active_only_leaves_role_untouched(app_client, test_db):
    async with session_scope() as db:
        db.add(User(id="u_active_only", username="activeonly", org_id="acme", role="operator", active=True))
        await db.flush()

    client, _ = app_client
    admin = _admin_headers(org_id="acme")
    r = await client.patch("/v1/users/u_active_only", headers=admin, json={"active": False})
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False
    assert r.json()["role"] == "operator"  # unchanged — role was never in the payload


async def test_resend_invite_rejects_an_already_accepted_account(app_client):
    client, _ = app_client
    admin = _admin_headers(org_id=None)
    invite = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "resend1", "email": "resend1@x.com"}
    )
    user_id = invite.json()["user"]["id"]
    token = invite.json()["invite_url"].split("?invite=")[1]
    await client.post("/v1/users/accept-invite", json={"token": token, "password": "whatever password"})

    again = await client.post(f"/v1/users/{user_id}/resend-invite", headers=admin)
    assert again.status_code == 409 and again.json()["code"] == "already_accepted"


async def test_resend_invite_regenerates_token_for_still_pending_account(app_client):
    client, _ = app_client
    admin = _admin_headers(org_id=None)
    invite = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "resend2", "email": "resend2@x.com"}
    )
    user_id = invite.json()["user"]["id"]
    first_token = invite.json()["invite_url"].split("?invite=")[1]

    again = await client.post(f"/v1/users/{user_id}/resend-invite", headers=admin)
    assert again.status_code == 200
    second_token = again.json()["invite_url"].split("?invite=")[1]
    assert second_token != first_token

    # The OLD link no longer works; the NEW one does.
    old = await client.post(
        "/v1/users/accept-invite", json={"token": first_token, "password": "irrelevant password"}
    )
    assert old.status_code == 400
    new = await client.post(
        "/v1/users/accept-invite", json={"token": second_token, "password": "irrelevant password"}
    )
    assert new.status_code == 200


async def test_accept_invite_rejects_expired_token(app_client, test_db):
    client, _ = app_client
    admin = _admin_headers(org_id=None)
    invite = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "expiredop", "email": "expired@x.com"}
    )
    user_id = invite.json()["user"]["id"]
    token = invite.json()["invite_url"].split("?invite=")[1]

    # Force it into the past.
    async with session_scope() as db:
        row = await db.get(User, user_id)
        row.invite_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    r = await client.post("/v1/users/accept-invite", json={"token": token, "password": "does not matter"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_or_expired_token"


async def test_accept_invite_rejects_malformed_and_wrong_verifier(app_client):
    client, _ = app_client
    admin = _admin_headers(org_id=None)
    invite = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "wrongtok", "email": "wrongtok@x.com"}
    )
    token = invite.json()["invite_url"].split("?invite=")[1]
    selector, _, _verifier = token.partition(".")

    malformed = await client.post(
        "/v1/users/accept-invite", json={"token": "no-dot-here", "password": "some password value"}
    )
    assert malformed.status_code == 400 and malformed.json()["code"] == "malformed_token"

    wrong_verifier = await client.post(
        "/v1/users/accept-invite",
        json={"token": f"{selector}.wrong-verifier-guess", "password": "some password value"},
    )
    assert wrong_verifier.status_code == 400 and wrong_verifier.json()["code"] == "invalid_or_expired_token"

    unknown_selector = await client.post(
        "/v1/users/accept-invite",
        json={"token": "totally-unknown-selector.whatever", "password": "some password value"},
    )
    assert unknown_selector.status_code == 400


async def test_patch_updates_role_and_active(app_client, test_db):
    client, _ = app_client
    async with session_scope() as db:
        db.add(User(id="u_patch", username="patchme", org_id="acme", role="operator"))
        await db.flush()
    admin = _admin_headers(org_id="acme")

    r = await client.patch("/v1/users/u_patch", headers=admin, json={"role": "admin", "active": False})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin" and r.json()["active"] is False


async def test_operator_role_is_forbidden_from_every_route(app_client):
    client, _ = app_client
    operator = _operator_headers(org_id="acme")
    assert (await client.get("/v1/users", headers=operator)).status_code == 403
    assert (
        await client.post("/v1/users/invite", headers=operator, json={"username": "x", "email": "x@x.com"})
    ).status_code == 403
    assert (await client.get("/v1/users/anything", headers=operator)).status_code == 403
    assert (
        await client.patch("/v1/users/anything", headers=operator, json={"active": False})
    ).status_code == 403
    assert (await client.post("/v1/users/anything/deactivate", headers=operator)).status_code == 403


async def test_users_require_auth(app_client):
    client, _ = app_client
    assert (await client.get("/v1/users")).status_code == 401
    assert (
        await client.post("/v1/users/invite", json={"username": "x", "email": "x@x.com"})
    ).status_code == 401


async def test_get_and_mutate_missing_user_are_404(app_client):
    client, _ = app_client
    admin = _admin_headers(org_id=None)
    assert (await client.get("/v1/users/nope", headers=admin)).status_code == 404
    assert (await client.patch("/v1/users/nope", headers=admin, json={"active": False})).status_code == 404
    assert (await client.post("/v1/users/nope/deactivate", headers=admin)).status_code == 404
    assert (await client.post("/v1/users/nope/reactivate", headers=admin)).status_code == 404
    assert (await client.post("/v1/users/nope/reset-password", headers=admin)).status_code == 404
    assert (await client.post("/v1/users/nope/resend-invite", headers=admin)).status_code == 404


async def test_invalid_email_and_username_are_422(app_client):
    client, _ = app_client
    admin = _admin_headers(org_id=None)
    bad_email = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "ok", "email": "not-an-email"}
    )
    assert bad_email.status_code == 422
    bad_username = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "has spaces", "email": "ok@x.com"}
    )
    assert bad_username.status_code == 422


# --- security-review regressions ---------------------------------------------


async def test_deactivate_revokes_the_token_and_accept_invite_cannot_resurrect_it(app_client):
    # CRITICAL fix: accept-invite must never silently reverse an explicit
    # deactivate_user call. Before the fix, a still-outstanding token (issued
    # before deactivation, valid for up to invite_ttl_hours) could be redeemed
    # on this PUBLIC, unauthenticated route to flip active back to True.
    client, _ = app_client
    admin = _admin_headers(org_id=None)
    invite = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "lockme", "email": "lockme@x.com"}
    )
    user_id = invite.json()["user"]["id"]
    token = invite.json()["invite_url"].split("?invite=")[1]

    deactivate = await client.post(f"/v1/users/{user_id}/deactivate", headers=admin)
    assert deactivate.status_code == 200 and deactivate.json()["active"] is False

    # The pre-deactivation invite link must be dead — not just "still pending".
    accept = await client.post(
        "/v1/users/accept-invite", json={"token": token, "password": "attacker chosen password"}
    )
    assert accept.status_code == 400 and accept.json()["code"] == "invalid_or_expired_token"

    # And the account is STILL deactivated — accept-invite did not touch it.
    still = await client.get(f"/v1/users/{user_id}", headers=admin)
    assert still.json()["active"] is False

    # Reactivating restores login capability but NOT the old (revoked) token —
    # the admin must issue a fresh one.
    reactivate = await client.post(f"/v1/users/{user_id}/reactivate", headers=admin)
    assert reactivate.status_code == 200 and reactivate.json()["active"] is True
    stale_again = await client.post(
        "/v1/users/accept-invite", json={"token": token, "password": "still should not work"}
    )
    assert stale_again.status_code == 400


async def test_deactivate_an_already_accepted_account_also_blocks_a_leftover_reset_token(app_client):
    # Same CRITICAL fix, via the reset-password path: an admin issues a reset
    # link, then (before it's redeemed) deactivates the account for cause — the
    # outstanding reset link must not be able to set a new password and revive it.
    client, _ = app_client
    admin = _admin_headers(org_id=None)
    invite = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "resetlock", "email": "resetlock@x.com"}
    )
    user_id = invite.json()["user"]["id"]
    first_token = invite.json()["invite_url"].split("?invite=")[1]
    await client.post(
        "/v1/users/accept-invite", json={"token": first_token, "password": "initial password value"}
    )

    reset = await client.post(f"/v1/users/{user_id}/reset-password", headers=admin)
    reset_token = reset.json()["invite_url"].split("?invite=")[1]

    await client.post(f"/v1/users/{user_id}/deactivate", headers=admin)

    r = await client.post(
        "/v1/users/accept-invite", json={"token": reset_token, "password": "attacker chosen password"}
    )
    assert r.status_code == 400 and r.json()["code"] == "invalid_or_expired_token"


async def test_cross_org_username_collision_is_rejected_without_duplicating_the_identity(app_client, test_db):
    # HIGH-finding-adjacent regression: a tenant-scoped admin's duplicate check
    # is now scoped to their OWN org (so it can't be used to enumerate another
    # tenant's identities via a broader query) — but the DB's global unique
    # constraint on username/email must still catch a genuine cross-org
    # collision via the IntegrityError fallback, so two orgs never end up with
    # two DIFFERENT rows sharing the same username (a data-integrity break,
    # not just a security one — e.g. login-by-username would become ambiguous).
    client, _ = app_client
    orgA_admin = _admin_headers(org_id="orgA")
    orgB_admin = _admin_headers(org_id="orgB")

    first = await client.post(
        "/v1/users/invite", headers=orgA_admin, json={"username": "sharedname", "email": "a@orgA.com"}
    )
    assert first.status_code == 201

    # orgB's admin doesn't see orgA's row (their own-org check finds nothing),
    # but the insert must still fail — the same username can't exist twice.
    second = await client.post(
        "/v1/users/invite", headers=orgB_admin, json={"username": "sharedname", "email": "b@orgB.com"}
    )
    assert second.status_code == 409 and second.json()["code"] == "user_exists"

    # Exactly one row with this username exists, and it's still orgA's.
    async with session_scope() as db:
        rows = (
            await db.execute(select(User).where(User.username == "sharedname"))
        ).scalars().all()
    assert len(rows) == 1 and rows[0].org_id == "orgA"


async def test_invite_url_is_omitted_once_the_email_actually_sends(app_client, monkeypatch):
    # #3/#4 fix: email_sent must reflect ACTUAL delivery, and the raw
    # (immediately-usable) link must not keep transiting the API response once
    # a real send succeeds — only the dormant/failed-send fallback returns it.
    from koel.voicebot.api import users as users_mod

    async def _fake_send_message(channel, to, body, **kwargs):  # noqa: ANN001, ANN003, ANN202
        return True

    monkeypatch.setattr(users_mod, "send_message", _fake_send_message)

    client, _ = app_client
    admin = _admin_headers(org_id=None)
    r = await client.post(
        "/v1/users/invite", headers=admin, json={"username": "emailed", "email": "emailed@x.com"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["email_sent"] is True
    assert body["invite_url"] is None
