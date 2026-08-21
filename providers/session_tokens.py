"""
Expiry-aware OAuth token handling for provider sessions.

Tokens live only in Streamlit's per-session memory. Nothing here writes tokens to
disk, logs them, or renders them: ``TokenSet`` deliberately redacts its own
``repr`` so an access or refresh token can never leak through a traceback, a
debug print, or Streamlit's exception display.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Tuple

from providers.base import (
    ProviderAuthenticationError,
    ProviderTokenExpiredError,
)

_REDACTED = "<redacted>"

# Refresh this many seconds before the stated expiry, so a request never goes out
# with a token that expires mid-flight.
DEFAULT_EXPIRY_LEEWAY_SECONDS = 60


@dataclass(repr=False)
class TokenSet:
    """A provider access token plus everything needed to renew it."""

    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None
    scopes: tuple = ()
    token_type: str = "Bearer"
    obtained_at: float = field(default_factory=time.time)

    @classmethod
    def from_response(
        cls,
        data: dict,
        previous: Optional["TokenSet"] = None,
        now: Optional[float] = None,
    ) -> "TokenSet":
        """
        Build a TokenSet from a raw OAuth token endpoint response.

        ``previous`` carries forward the refresh token: Google (and often Entra)
        omit ``refresh_token`` from refresh responses, and dropping it would
        silently turn a renewable session into a one-shot one.
        """
        if not isinstance(data, dict):
            raise ProviderAuthenticationError("The token endpoint returned an unexpected response.")

        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise ProviderAuthenticationError(
                "The token endpoint response did not include an access token."
            )

        issued = time.time() if now is None else now

        expires_at = None
        expires_in = data.get("expires_in")
        try:
            if expires_in is not None:
                expires_at = issued + float(expires_in)
        except (TypeError, ValueError):
            expires_at = None

        refresh_token = str(data.get("refresh_token") or "").strip() or None
        if refresh_token is None and previous is not None:
            refresh_token = previous.refresh_token

        raw_scope = data.get("scope") or ""
        if isinstance(raw_scope, (list, tuple)):
            scopes = tuple(str(s) for s in raw_scope if s)
        else:
            scopes = tuple(str(raw_scope).split())
        if not scopes and previous is not None:
            scopes = previous.scopes

        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=scopes,
            token_type=str(data.get("token_type") or "Bearer"),
            obtained_at=issued,
        )

    def seconds_remaining(self, now: Optional[float] = None) -> Optional[float]:
        if self.expires_at is None:
            return None
        current = time.time() if now is None else now
        return self.expires_at - current

    def is_expired(
        self,
        leeway: float = DEFAULT_EXPIRY_LEEWAY_SECONDS,
        now: Optional[float] = None,
    ) -> bool:
        """True when the token is past (or about to pass) its stated expiry."""
        remaining = self.seconds_remaining(now)
        if remaining is None:
            # No expiry advertised: assume usable and rely on a 401 to correct us.
            return False
        return remaining <= leeway

    def can_refresh(self) -> bool:
        return bool(self.refresh_token)

    def public_summary(self) -> dict:
        """Non-secret facts about this session, safe to render in the UI."""
        remaining = self.seconds_remaining()
        return {
            "token_type": self.token_type,
            "scopes": list(self.scopes),
            "has_refresh_token": bool(self.refresh_token),
            "expires_in_seconds": None if remaining is None else max(0, int(remaining)),
            "expired": self.is_expired(),
        }

    def __repr__(self) -> str:
        return (
            "TokenSet(access_token={r}, refresh_token={r}, token_type={t!r}, "
            "expires_at={e!r}, scopes={s!r})"
        ).format(r=_REDACTED, t=self.token_type, e=self.expires_at, s=self.scopes)

    def __str__(self) -> str:
        return self.__repr__()


def refresh_tokens(provider, tokens: TokenSet) -> TokenSet:
    """Exchange the refresh token for a new access token."""
    if not tokens.can_refresh():
        raise ProviderAuthenticationError(
            "This session has no refresh token, so it cannot be renewed. Please reconnect."
        )
    raw = provider.refresh_access_token(tokens.refresh_token)
    return TokenSet.from_response(raw, previous=tokens)


def call_with_refresh(
    provider,
    tokens: TokenSet,
    operation: Callable[[str], Any],
) -> Tuple[Any, TokenSet]:
    """
    Run ``operation(access_token)``, renewing the token once if needed.

    Refreshes up front when the token is known to be expired, and once more
    reactively if the provider answers 401 anyway (clock skew, revoked token,
    or an expiry the endpoint never advertised).

    Returns ``(result, tokens)`` where ``tokens`` may be a newer TokenSet that
    the caller should store back into the session.
    """
    current = tokens

    if current.is_expired() and current.can_refresh():
        current = refresh_tokens(provider, current)

    try:
        return operation(current.access_token), current
    except ProviderTokenExpiredError:
        if not current.can_refresh():
            raise
        current = refresh_tokens(provider, current)
        return operation(current.access_token), current
