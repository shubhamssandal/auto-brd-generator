"""
Stateless, signed OAuth handshake values (CSRF state + PKCE).

Why the state is signed rather than stored
------------------------------------------
Streamlit rebuilds its session when the browser follows the provider's redirect
back to the app, so anything written to ``st.session_state`` *before* the
redirect is gone by the time the ``?code=`` callback is handled. A nonce kept
server-side therefore cannot be compared on return.

Instead the state parameter carries its own proof: an HMAC-SHA256 signature over
``provider:nonce:issued_at``, keyed by a secret that never leaves this process.
An attacker cannot forge a state value without that key, which is the property
CSRF protection actually requires -- unlike a constant, guessable marker.

The PKCE code verifier is *derived* from the same key plus the nonce, so it can
be recomputed when the callback arrives yet never travels in any URL. The nonce
is public; the key is not, so the verifier stays secret.

Nothing in this module logs, prints, or renders secret material.
"""

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

# How long an issued authorization state remains acceptable.
STATE_TTL_SECONDS = 900

_SEPARATOR = "."

# Per-process signing key. Regenerated on restart, which bounds the lifetime of
# any outstanding state value. Set OAUTH_STATE_SECRET to keep states valid
# across restarts (optional; the name only ever appears in docs, never a value).
_PROCESS_KEY = secrets.token_bytes(32)


class OAuthStateError(Exception):
    """Raised when an OAuth state parameter is missing, forged, or expired."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signing_key() -> bytes:
    configured = os.getenv("OAUTH_STATE_SECRET", "").strip()
    if configured:
        return hashlib.sha256(configured.encode("utf-8")).digest()
    return _PROCESS_KEY


def _sign(payload: bytes) -> str:
    return _b64e(hmac.new(_signing_key(), payload, hashlib.sha256).digest())


def _derive_code_verifier(nonce: str) -> str:
    """Derive an RFC 7636 code verifier from the public nonce and the secret key."""
    material = hmac.new(_signing_key(), ("pkce:" + nonce).encode("utf-8"), hashlib.sha256).digest()
    # 43 characters, inside RFC 7636's 43-128 range, unreserved alphabet only.
    return _b64e(material)


@dataclass(frozen=True, repr=False)
class OAuthHandshake:
    """One authorization attempt: the state to send and the PKCE secret to keep."""

    provider: str
    state: str
    code_verifier: str
    issued_at: float

    @property
    def code_challenge(self) -> str:
        digest = hashlib.sha256(self.code_verifier.encode("ascii")).digest()
        return _b64e(digest)

    @property
    def code_challenge_method(self) -> str:
        return "S256"

    def is_stale(self, now: Optional[float] = None) -> bool:
        current = time.time() if now is None else now
        return (current - self.issued_at) >= STATE_TTL_SECONDS

    def __repr__(self) -> str:
        # The verifier is secret; keep it out of tracebacks and debug output.
        return "OAuthHandshake(provider={!r}, issued_at={!r}, code_verifier=<redacted>)".format(
            self.provider, self.issued_at
        )


def begin_handshake(provider: str, now: Optional[float] = None) -> OAuthHandshake:
    """Issue a fresh signed state and its matching PKCE verifier for ``provider``."""
    if not provider or ":" in provider:
        raise ValueError("provider must be a non-empty name without ':'")

    issued_at = float(int(time.time() if now is None else now))
    nonce = _b64e(secrets.token_bytes(24))
    payload = "{}:{}:{}".format(provider, nonce, int(issued_at)).encode("utf-8")
    state = "{}{}{}".format(_b64e(payload), _SEPARATOR, _sign(payload))

    return OAuthHandshake(
        provider=provider,
        state=state,
        code_verifier=_derive_code_verifier(nonce),
        issued_at=issued_at,
    )


def resolve_handshake(
    state: Optional[str],
    expected_provider: Optional[str] = None,
    now: Optional[float] = None,
) -> OAuthHandshake:
    """
    Verify a returned state parameter and rebuild its handshake.

    Raises OAuthStateError if the state is absent, malformed, unsigned by this
    process, past its TTL, or issued for a different provider.
    """
    if not state:
        raise OAuthStateError("no state parameter was returned")

    encoded_payload, _, signature = state.rpartition(_SEPARATOR)
    if not encoded_payload or not signature:
        raise OAuthStateError("state parameter is malformed")

    try:
        payload = _b64d(encoded_payload)
    except Exception:
        raise OAuthStateError("state parameter is malformed") from None

    # Constant-time comparison: never leak signature bytes through timing.
    if not hmac.compare_digest(_sign(payload), signature):
        raise OAuthStateError("state signature did not verify")

    try:
        provider, nonce, issued_raw = payload.decode("utf-8").split(":")
        issued_at = float(int(issued_raw))
    except Exception:
        raise OAuthStateError("state payload is malformed") from None

    current = time.time() if now is None else now
    if (current - issued_at) >= STATE_TTL_SECONDS:
        raise OAuthStateError("state parameter has expired")
    if current + 60 < issued_at:
        raise OAuthStateError("state parameter is not yet valid")
    if expected_provider is not None and provider != expected_provider:
        raise OAuthStateError("state parameter was issued for a different provider")

    return OAuthHandshake(
        provider=provider,
        state=state,
        code_verifier=_derive_code_verifier(nonce),
        issued_at=issued_at,
    )
