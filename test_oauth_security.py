"""
OAuth handshake and session-token tests.

Nothing here contacts a real identity provider: every token endpoint call is
monkeypatched. No real Google or Microsoft credentials are required.
"""

import base64
import hashlib
import time

import pytest

from providers.base import (
    ProviderAuthenticationError,
    ProviderTokenExpiredError,
)
from providers.oauth_state import (
    STATE_TTL_SECONDS,
    OAuthStateError,
    begin_handshake,
    resolve_handshake,
)
from providers.session_tokens import TokenSet, call_with_refresh, refresh_tokens


# --- Signed OAuth state (CSRF protection) ---

def test_state_round_trips_and_carries_provider():
    handshake = begin_handshake("google_meet")
    resolved = resolve_handshake(handshake.state)

    assert resolved.provider == "google_meet"
    assert resolved.code_verifier == handshake.code_verifier
    assert resolved.is_stale() is False


def test_state_is_not_a_guessable_constant():
    """The old implementation sent the literal provider name; that is not CSRF protection."""
    first = begin_handshake("google_meet").state
    second = begin_handshake("google_meet").state

    assert first != second
    assert first != "google_meet"
    assert len(first) > 40


def test_forged_state_is_rejected():
    with pytest.raises(OAuthStateError, match="malformed"):
        resolve_handshake("google_meet")

    forged_payload = base64.urlsafe_b64encode(
        b"google_meet:attacker-nonce:" + str(int(time.time())).encode()
    ).decode().rstrip("=")
    with pytest.raises(OAuthStateError, match="signature did not verify"):
        resolve_handshake(f"{forged_payload}.not-a-real-signature")


def test_tampered_state_payload_is_rejected():
    handshake = begin_handshake("google_meet")
    payload, _, signature = handshake.state.rpartition(".")

    tampered = base64.urlsafe_b64encode(
        b"microsoft_teams:swapped:" + str(int(time.time())).encode()
    ).decode().rstrip("=")

    with pytest.raises(OAuthStateError, match="signature did not verify"):
        resolve_handshake(f"{tampered}.{signature}")

    # Flipping a signature character must also fail.
    broken_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(OAuthStateError, match="signature did not verify"):
        resolve_handshake(f"{payload}.{broken_signature}")


def test_missing_state_is_rejected():
    with pytest.raises(OAuthStateError, match="no state parameter"):
        resolve_handshake("")
    with pytest.raises(OAuthStateError, match="no state parameter"):
        resolve_handshake(None)


def test_expired_state_is_rejected():
    issued = time.time() - (STATE_TTL_SECONDS + 30)
    handshake = begin_handshake("google_meet", now=issued)

    with pytest.raises(OAuthStateError, match="expired"):
        resolve_handshake(handshake.state)

    assert handshake.is_stale() is True


def test_state_for_wrong_provider_is_rejected():
    handshake = begin_handshake("microsoft_teams")
    resolve_handshake(handshake.state, expected_provider="microsoft_teams")

    with pytest.raises(OAuthStateError, match="different provider"):
        resolve_handshake(handshake.state, expected_provider="google_meet")


def test_handshake_repr_hides_the_code_verifier():
    handshake = begin_handshake("google_meet")
    assert handshake.code_verifier not in repr(handshake)
    assert "redacted" in repr(handshake)


# --- PKCE ---

def test_pkce_challenge_is_the_s256_hash_of_the_verifier():
    handshake = begin_handshake("google_meet")

    expected = base64.urlsafe_b64encode(
        hashlib.sha256(handshake.code_verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")

    assert handshake.code_challenge == expected
    assert handshake.code_challenge_method == "S256"
    assert "=" not in handshake.code_challenge


def test_pkce_verifier_length_is_rfc_compliant():
    verifier = begin_handshake("google_meet").code_verifier
    assert 43 <= len(verifier) <= 128


def test_pkce_verifier_never_appears_in_the_state():
    handshake = begin_handshake("google_meet")
    assert handshake.code_verifier not in handshake.state


# --- TokenSet ---

def test_token_set_from_response_reads_expiry_and_scopes():
    now = 1_000_000.0
    tokens = TokenSet.from_response(
        {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_in": 3600,
            "scope": "openid https://www.googleapis.com/auth/drive.readonly",
            "token_type": "Bearer",
        },
        now=now,
    )

    assert tokens.expires_at == now + 3600
    assert tokens.can_refresh() is True
    assert "openid" in tokens.scopes
    assert tokens.is_expired(now=now) is False
    assert tokens.is_expired(now=now + 3600) is True


def test_token_set_requires_an_access_token():
    with pytest.raises(ProviderAuthenticationError, match="did not include an access token"):
        TokenSet.from_response({"refresh_token": "rt-only"})


def test_token_set_carries_forward_refresh_token_and_scopes():
    """Google omits the refresh token on refresh responses; it must not be lost."""
    original = TokenSet.from_response(
        {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600, "scope": "a b"}
    )
    renewed = TokenSet.from_response({"access_token": "at-2", "expires_in": 3600}, previous=original)

    assert renewed.access_token == "at-2"
    assert renewed.refresh_token == "rt-1"
    assert renewed.scopes == original.scopes


def test_token_set_without_expiry_is_not_treated_as_expired():
    tokens = TokenSet.from_response({"access_token": "at-1"})
    assert tokens.seconds_remaining() is None
    assert tokens.is_expired() is False


def test_token_set_never_reveals_secrets():
    tokens = TokenSet.from_response(
        {"access_token": "super-secret-access", "refresh_token": "super-secret-refresh",
         "expires_in": 3600, "scope": "openid"}
    )

    text = repr(tokens) + str(tokens)
    assert "super-secret-access" not in text
    assert "super-secret-refresh" not in text

    summary = tokens.public_summary()
    assert summary["has_refresh_token"] is True
    assert summary["expires_in_seconds"] <= 3600
    assert "access_token" not in summary
    assert "refresh_token" not in summary
    assert "super-secret-access" not in str(summary)


# --- Refresh behaviour ---

class _StubProvider:
    """Minimal stand-in for a provider's refresh endpoint."""

    def __init__(self, new_access_token="at-refreshed"):
        self.refresh_calls = []
        self._new_access_token = new_access_token

    def refresh_access_token(self, refresh_token):
        self.refresh_calls.append(refresh_token)
        return {"access_token": self._new_access_token, "expires_in": 3600}


def test_refresh_tokens_requires_a_refresh_token():
    tokens = TokenSet.from_response({"access_token": "at-1"})
    with pytest.raises(ProviderAuthenticationError, match="no refresh token"):
        refresh_tokens(_StubProvider(), tokens)


def test_call_with_refresh_refreshes_proactively_when_expired():
    provider = _StubProvider()
    expired = TokenSet.from_response(
        {"access_token": "at-old", "refresh_token": "rt-1", "expires_in": 1},
        now=time.time() - 600,
    )

    used_tokens = []
    result, tokens = call_with_refresh(provider, expired, lambda token: used_tokens.append(token) or "ok")

    assert result == "ok"
    assert provider.refresh_calls == ["rt-1"]
    assert used_tokens == ["at-refreshed"]
    assert tokens.access_token == "at-refreshed"
    assert tokens.refresh_token == "rt-1"


def test_call_with_refresh_retries_once_on_401():
    provider = _StubProvider()
    tokens = TokenSet.from_response({"access_token": "at-old", "refresh_token": "rt-1"})
    attempts = []

    def operation(token):
        attempts.append(token)
        if len(attempts) == 1:
            raise ProviderTokenExpiredError("token rejected")
        return "recovered"

    result, updated = call_with_refresh(provider, tokens, operation)

    assert result == "recovered"
    assert attempts == ["at-old", "at-refreshed"]
    assert len(provider.refresh_calls) == 1
    assert updated.access_token == "at-refreshed"


def test_call_with_refresh_gives_up_when_no_refresh_token():
    provider = _StubProvider()
    tokens = TokenSet.from_response({"access_token": "at-only"})

    def operation(token):
        raise ProviderTokenExpiredError("token rejected")

    with pytest.raises(ProviderTokenExpiredError):
        call_with_refresh(provider, tokens, operation)
    assert provider.refresh_calls == []


def test_call_with_refresh_does_not_loop_forever():
    """A second 401 after refreshing must surface, not trigger endless retries."""
    provider = _StubProvider()
    tokens = TokenSet.from_response({"access_token": "at-old", "refresh_token": "rt-1"})

    attempts = []

    def operation(token):
        attempts.append(token)
        raise ProviderTokenExpiredError("still rejected")

    with pytest.raises(ProviderTokenExpiredError):
        call_with_refresh(provider, tokens, operation)

    assert len(attempts) == 2
    assert len(provider.refresh_calls) == 1
