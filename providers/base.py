from abc import ABC, abstractmethod
from typing import Any, Optional
from brd_models import NormalizedTranscript


class ProviderError(Exception):
    """Base exception for provider-related errors."""
    pass


class ProviderNotConfiguredError(ProviderError):
    """Raised when an operation is attempted on an unconfigured provider."""
    pass


class ProviderAuthenticationError(ProviderError):
    """Raised when authentication with the provider fails."""
    pass


class ProviderTokenExpiredError(ProviderAuthenticationError):
    """
    Raised on HTTP 401 from a provider API.

    Distinct from a generic authentication failure because it is recoverable:
    the caller can refresh the access token and retry the same request once.
    """
    pass


class ProviderConsentRequiredError(ProviderAuthenticationError):
    """
    Raised on HTTP 403 when the caller is authenticated but not permitted.

    Typically a missing scope, an un-granted tenant admin consent, or an API that
    has not been enabled. Refreshing the token will not help; the fix is external
    configuration, so this must not be retried.
    """
    pass


class ProviderAPIError(ProviderError):
    """Raised when a provider API call fails for a non-authorization reason."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class TranscriptUnavailableError(ProviderError):
    """
    Raised when the provider is reachable and authorized, but there is simply no
    transcript to return: transcription was never enabled, the file has not been
    generated yet, or the meeting produced no captured speech.
    """
    pass


class TranscriptParseError(ProviderError):
    """Raised when transcript content is retrieved but cannot be parsed."""
    pass


class TranscriptProvider(ABC):
    """
    Abstract base class for meeting transcript providers.

    Discovery contract
    ------------------
    ``list_available_transcripts`` returns a list of plain dicts so the UI can
    render any provider's results with one shared component. Each entry uses:

    ``id``            provider-specific identifier to pass back to ``get_transcript``
    ``title``         real meeting title if the provider supplies one, else None
    ``date``          ISO-8601 timestamp string if known, else None
    ``display_label`` short human-readable label for a picker
    ``source_type``   which provider path produced the entry (e.g. ``meet_api``)
    ``participants``  list of real participant names, empty when unknown
    ``details``       dict of extra facts to display verbatim
    ``available``     False when the transcript exists but is not yet retrievable

    Titles, dates, and participants must reflect what the provider actually
    returned. Unknown fields stay None or empty rather than being invented.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier name."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable provider name."""
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        """Check whether the necessary configuration/credentials are present."""
        pass

    @abstractmethod
    def get_missing_configuration(self) -> list[str]:
        """Return a list of missing configuration keys or items."""
        pass

    @abstractmethod
    def get_setup_instructions(self) -> str:
        """Return detailed instructions for configuring this provider."""
        pass

    @abstractmethod
    def get_authorization_url(
        self,
        redirect_uri: str,
        state: Optional[str] = None,
        code_challenge: Optional[str] = None,
        code_challenge_method: str = "S256",
    ) -> str:
        """Builds the OAuth 2.0 authorization URL for user consent."""
        pass

    @abstractmethod
    def exchange_code_for_token(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: Optional[str] = None,
    ) -> dict[str, Any]:
        """Exchanges an authorization code for an OAuth access token dictionary."""
        pass

    @abstractmethod
    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Exchanges a refresh token for a fresh OAuth access token dictionary."""
        pass

    @abstractmethod
    def list_available_transcripts(self, access_token: Optional[str] = None) -> list[dict[str, Any]]:
        """List available meeting transcripts from the provider."""
        pass

    @abstractmethod
    def get_transcript(self, meeting_id: str, access_token: Optional[str] = None) -> NormalizedTranscript:
        """Fetch and normalize a specific meeting transcript."""
        pass

    def get_user_profile(self, access_token: str) -> dict[str, Any]:
        """
        Best-effort identity of the connected account, for showing who is signed
        in. Returns an empty dict when unavailable; never raises.
        """
        return {}

    def discover_transcripts(self, access_token: Optional[str] = None, **kwargs) -> dict[str, Any]:
        """
        Richer counterpart to ``list_available_transcripts``.

        Returns ``{"transcripts": [...], "truncated": bool, "notes": [str, ...]}``
        so the UI can tell the difference between "nothing found" and "results
        were capped", and can surface per-meeting problems without failing the
        whole discovery run. Default implementation reports no truncation.
        """
        return {
            "transcripts": self.list_available_transcripts(access_token=access_token),
            "truncated": False,
            "notes": [],
        }
