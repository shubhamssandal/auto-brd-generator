import os
import re
import urllib.parse
from typing import Any, Optional
import requests
from brd_models import NormalizedTranscript
from providers.base import (
    TranscriptProvider,
    ProviderNotConfiguredError,
    ProviderAuthenticationError,
    ProviderError,
)


class MSTeamsProvider(TranscriptProvider):
    """
    Real provider adaptor for Microsoft Teams meeting transcripts via Microsoft Graph API.
    Reads credentials securely from environment variables.
    """

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    DEFAULT_SCOPES = [
        "https://graph.microsoft.com/OnlineMeetings.Read",
        "https://graph.microsoft.com/OnlineMeetingTranscript.Read.All",
        "offline_access",
        "User.Read",
    ]

    def _get_client_id(self) -> str:
        return os.getenv("AZURE_CLIENT_ID", "").strip()

    def _get_client_secret(self) -> str:
        return os.getenv("AZURE_CLIENT_SECRET", "").strip()

    def _get_tenant_id(self) -> str:
        return os.getenv("AZURE_TENANT_ID", "").strip() or "common"

    @property
    def name(self) -> str:
        return "microsoft_teams"

    @property
    def display_name(self) -> str:
        return "Microsoft Teams"

    def is_configured(self) -> bool:
        """Checks if all required Azure / Teams credentials exist."""
        return bool(self._get_client_id() and self._get_client_secret() and os.getenv("AZURE_TENANT_ID", "").strip())

    def get_missing_configuration(self) -> list[str]:
        """Returns missing configuration variable names."""
        missing = []
        if not self._get_client_id():
            missing.append("AZURE_CLIENT_ID")
        if not os.getenv("AZURE_TENANT_ID", "").strip():
            missing.append("AZURE_TENANT_ID")
        if not self._get_client_secret():
            missing.append("AZURE_CLIENT_SECRET")
        return missing

    def get_setup_instructions(self) -> str:
        """Detailed instructions for configuring Microsoft Teams integration."""
        return (
            "### Microsoft Teams & Entra ID Configuration Prerequisites\n\n"
            "To enable live Microsoft Teams transcript retrieval, configure the following:\n\n"
            "1. **Azure App Registration**: In Microsoft Entra ID (Azure AD portal), register a new application.\n"
            "2. **API Permissions** (Microsoft Graph):\n"
            "   - `OnlineMeetings.Read` (or `OnlineMeetings.ReadWrite`)\n"
            "   - `OnlineMeetingTranscript.Read.All`\n"
            "   - `User.Read`\n"
            "3. **Admin Consent**: A Microsoft 365 Tenant Administrator must grant admin consent for these permissions.\n"
            "4. **Teams Meeting Policy**: Ensure cloud recording and meeting transcription are enabled in the Teams admin center.\n"
            "5. **Environment Variables**: Add to your `.env` file:\n"
            "   ```env\n"
            "   AZURE_CLIENT_ID=\"your-azure-application-client-id\"\n"
            "   AZURE_TENANT_ID=\"your-tenant-id-or-directory-id\"\n"
            "   AZURE_CLIENT_SECRET=\"your-azure-client-secret-value\"\n"
            "   AZURE_REDIRECT_URI=\"http://localhost:8501\"\n"
            "   ```\n"
        )

    def get_authorization_url(self, redirect_uri: str, state: Optional[str] = None) -> str:
        """Generates the Microsoft Entra ID OAuth 2.0 authorization URL."""
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                f"Microsoft Teams is not configured. Missing: {', '.join(self.get_missing_configuration())}"
            )

        tenant = self._get_tenant_id()
        auth_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"

        params = {
            "client_id": self._get_client_id(),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.DEFAULT_SCOPES),
            "response_mode": "query",
            "prompt": "select_account",
        }
        if state:
            params["state"] = state

        return f"{auth_url}?{urllib.parse.urlencode(params)}"

    def exchange_code_for_token(self, code: str, redirect_uri: str) -> dict[str, Any]:
        """Exchanges authorization code for Microsoft Graph access tokens."""
        if not self.is_configured():
            raise ProviderNotConfiguredError("Microsoft Teams provider is not configured.")

        tenant = self._get_tenant_id()
        token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

        payload = {
            "client_id": self._get_client_id(),
            "client_secret": self._get_client_secret(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.DEFAULT_SCOPES),
        }

        try:
            response = requests.post(token_url, data=payload, timeout=15)
            data = response.json()
            if response.status_code != 200:
                err_desc = data.get("error_description", data.get("error", "Microsoft token exchange failed"))
                raise ProviderAuthenticationError(f"Microsoft OAuth failed: {err_desc}")
            return data
        except requests.RequestException as e:
            raise ProviderAuthenticationError(f"Network error during Microsoft token exchange: {e}")

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Refreshes an expired Microsoft Graph access token."""
        if not self.is_configured():
            raise ProviderNotConfiguredError("Microsoft Teams provider is not configured.")

        tenant = self._get_tenant_id()
        token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

        payload = {
            "client_id": self._get_client_id(),
            "client_secret": self._get_client_secret(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": " ".join(self.DEFAULT_SCOPES),
        }

        try:
            response = requests.post(token_url, data=payload, timeout=15)
            data = response.json()
            if response.status_code != 200:
                err_desc = data.get("error_description", data.get("error", "Microsoft token refresh failed"))
                raise ProviderAuthenticationError(f"Microsoft token refresh failed: {err_desc}")
            return data
        except requests.RequestException as e:
            raise ProviderAuthenticationError(f"Network error during Microsoft token refresh: {e}")

    def list_available_transcripts(self, access_token: Optional[str] = None) -> list[dict[str, Any]]:
        """Lists online meetings with available transcripts via Microsoft Graph."""
        if not self.is_configured():
            raise ProviderNotConfiguredError("Microsoft Teams provider is not configured.")
        if not access_token:
            raise ProviderAuthenticationError("An active access token is required to list Teams transcripts.")

        headers = {"Authorization": f"Bearer {access_token}"}
        transcripts: list[dict[str, Any]] = []

        try:
            # Query recent online meetings
            res = requests.get(f"{self.GRAPH_BASE}/me/onlineMeetings", headers=headers, timeout=15)
            if res.status_code == 200:
                meetings = res.json().get("value", [])
                for m in meetings:
                    m_id = m.get("id")
                    m_subject = m.get("subject", "Teams Meeting")
                    # Check for transcripts on this meeting
                    t_res = requests.get(f"{self.GRAPH_BASE}/me/onlineMeetings/{m_id}/transcripts", headers=headers, timeout=15)
                    if t_res.status_code == 200:
                        for t in t_res.json().get("value", []):
                            transcripts.append({
                                "id": f"{m_id}/transcripts/{t.get('id')}",
                                "title": m_subject,
                                "date": t.get("createdDateTime", m.get("startDateTime", "N/A")),
                                "source_type": "ms_graph_transcript",
                            })
            elif res.status_code in (401, 403):
                err_json = res.json().get("error", {})
                raise ProviderAuthenticationError(
                    f"Graph API permission error ({res.status_code}): {err_json.get('message', res.text)}. "
                    "Ensure admin consent is granted for OnlineMeetingTranscript.Read.All."
                )
        except requests.RequestException as e:
            raise ProviderError(f"Failed to query Microsoft Graph meetings: {e}")

        return transcripts

    def _parse_vtt_content(self, vtt_text: str) -> str:
        """Parses WebVTT format into clean readable transcript text."""
        lines = []
        for line in vtt_text.splitlines():
            line_str = line.strip()
            if (
                not line_str
                or line_str.startswith("WEBVTT")
                or "-->" in line_str
                or line_str.isdigit()
                or line_str.startswith("NOTE")
            ):
                continue
            # Convert <v Speaker Name>text</v> to "Speaker Name: text"
            cleaned = re.sub(r"<v\s+([^>]+)>", r"\1: ", line_str)
            cleaned = re.sub(r"</v>", "", cleaned)
            # Remove remaining inline tags like <b>, </i>, <c>, etc.
            cleaned = re.sub(r"<[^>]+>", "", cleaned)
            cleaned = cleaned.strip()
            if cleaned:
                lines.append(cleaned)
        return "\n".join(lines).strip()


    def get_transcript(self, meeting_id: str, access_token: Optional[str] = None) -> NormalizedTranscript:
        """Retrieves and normalizes a transcript from Microsoft Graph."""
        if not self.is_configured():
            raise ProviderNotConfiguredError("Microsoft Teams provider is not configured.")
        if not access_token:
            raise ProviderAuthenticationError("An active access token is required to retrieve transcripts.")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/vtt, text/plain, application/json",
        }

        # meeting_id could be format "{onlineMeetingId}/transcripts/{transcriptId}"
        if "/transcripts/" in meeting_id:
            url = f"{self.GRAPH_BASE}/me/onlineMeetings/{meeting_id}/content"
        else:
            url = f"{self.GRAPH_BASE}/me/onlineMeetings/{meeting_id}/transcripts"

        try:
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code != 200:
                raise ProviderError(f"Failed to fetch Teams transcript ({res.status_code}): {res.text}")

            content_type = res.headers.get("Content-Type", "")
            if "vtt" in content_type or res.text.startswith("WEBVTT"):
                raw_text = self._parse_vtt_content(res.text)
            else:
                raw_text = res.text.strip()

            if not raw_text:
                raise ProviderError("The retrieved Microsoft Teams transcript is empty.")

            return NormalizedTranscript(
                raw_text=raw_text,
                source="microsoft_teams",
                meeting_title="Teams Meeting Transcript",
                metadata={"meeting_resource": meeting_id, "char_count": len(raw_text)},
            )
        except requests.RequestException as e:
            raise ProviderError(f"Network error fetching Teams transcript: {e}")
