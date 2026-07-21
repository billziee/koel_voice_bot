"""Split invite/password-reset tokens (selector + hashed verifier).

Used by ``koel.voicebot.api.users`` for account provisioning and admin-triggered
password resets. The token handed to the user is ``{selector}.{verifier}``:
``selector`` is a plaintext, indexed lookup key (finds the candidate row in
O(1) without a full-table scan or a timing-unsafe compare-every-row loop);
``verifier`` is the actual secret — only its sha256 digest is ever persisted,
so a database leak cannot be turned back into a usable token. Standard
"split token" pattern (Laravel's password-reset tokens, Django's
``default_token_generator`` family use the same shape for the same reason).

Both parts are drawn from ``secrets.token_urlsafe`` (CSPRNG, URL-safe), so the
combined token is safe to embed directly in a query string.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_SELECTOR_BYTES = 16  # 128 bits — plenty to make selector collisions negligible
_VERIFIER_BYTES = 32  # 256 bits — the actual secret; this is what must resist guessing


def generate_token() -> tuple[str, str, str]:
    """Return ``(selector, verifier, combined_token)`` for a fresh invite/reset."""
    selector = secrets.token_urlsafe(_SELECTOR_BYTES)
    verifier = secrets.token_urlsafe(_VERIFIER_BYTES)
    return selector, verifier, f"{selector}.{verifier}"


def hash_verifier(verifier: str) -> str:
    """sha256 hex digest of the verifier — this, never the verifier itself, is stored."""
    return hashlib.sha256(verifier.encode()).hexdigest()


def verify_verifier(verifier: str, expected_hash: str) -> bool:
    """Constant-time compare of a presented verifier against its stored hash."""
    return hmac.compare_digest(hash_verifier(verifier), expected_hash)


def split_token(token: str) -> tuple[str, str] | None:
    """``"{selector}.{verifier}"`` → ``(selector, verifier)``, or None if malformed."""
    selector, sep, verifier = token.partition(".")
    if not sep or not selector or not verifier:
        return None
    return selector, verifier
