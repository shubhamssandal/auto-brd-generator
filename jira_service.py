"""
Jira Cloud OAuth 2.0 (3LO) connection.

Scope of this module
--------------------
Obtaining and renewing an authorization for a Jira Cloud account, identifying it,
and listing the Jira Cloud sites that authorization can reach. Project discovery,
Jira metadata and issue creation are deliberately absent: they arrive in their own
tickets, and keeping them out means a connected session here cannot write anything
to Jira. Every request this module makes is a GET.

Why this is not a TranscriptProvider
------------------------------------
Jira is not a transcript source, so subclassing ``TranscriptProvider`` would
force meaningless ``list_available_transcripts``/``get_transcript`` methods. The
OAuth helpers in ``main.py`` are duck-typed, so this class only implements the
handful of methods they actually call and reuses the shared signed-state
handshake, ``TokenSet`` and provider error hierarchy unchanged.

Where Atlassian differs from Google and Microsoft
-------------------------------------------------
1. The token endpoint takes a JSON body, not a form-encoded one, so the existing
   ``_post_token_request`` helpers cannot be reused verbatim.
2. Atlassian's 3LO documentation defines only ``audience``, ``client_id``,
   ``scope``, ``redirect_uri``, ``state``, ``response_type`` and ``prompt`` on
   the authorization request. PKCE is not part of that contract, so no
   ``code_challenge`` is sent (see ``get_authorization_url``).
3. Refresh tokens rotate: each refresh disables the token that was used, so the
   replacement must be stored. ``TokenSet.from_response(..., previous=...)``
   already does this for every provider.

Nothing here logs, prints, or returns a token, an authorization code, or the
client secret.
"""

import urllib.parse
from typing import Any, Optional

import requests

import jira_config
from jira_models import JiraSite
from providers.base import (
    ProviderAPIError,
    ProviderAuthenticationError,
    ProviderConsentRequiredError,
    ProviderTokenExpiredError,
)


class JiraService:
    """The Jira Cloud OAuth connection used by the optional Jira section."""

    # Documented 3LO endpoints. The accessible-resources host is api.atlassian.com:
    # one sentence in Atlassian's own docs says auth.atlassian.com, but every
    # executable example on that same page uses api.atlassian.com.
    AUTH_URL = "https://auth.atlassian.com/authorize"
    TOKEN_URL = "https://auth.atlassian.com/oauth/token"
    ME_URL = "https://api.atlassian.com/me"
    ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

    # Atlassian requires this audience on the authorization request.
    AUDIENCE = "api.atlassian.com"

    # The Jira scope that makes a site appear in accessible-resources at all. The
    # endpoint "retrieve[s] the sites that have scopes granted by the token", so a
    # token holding only account-level scopes lists no Jira site, however many the
    # user owns. read:jira-user is the narrower of the two documented read-only
    # classic Jira scopes -- read:jira-work also grants project and issue data,
    # which nothing in this ticket reads.
    SITE_SCOPE = "read:jira-user"

    # Least privilege for this ticket: the connected account's identity so the UI
    # can say who is signed in, one read-only Jira scope so accessible sites can
    # be listed at all, and a refresh token. Nothing here can write to Jira.
    SCOPES = [
        "read:me",
        SITE_SCOPE,
        "offline_access",
    ]

    TIMEOUT_SECONDS = 30

    # --- Identity for the shared UI helpers --------------------------------

    @property
    def name(self) -> str:
        """Session-state namespace and OAuth state routing key."""
        return "jira"

    @property
    def display_name(self) -> str:
        return jira_config.DISPLAY_NAME

    # The shared connect button's default caption promises a PKCE challenge,
    # which would be untrue here. Overriding it keeps the UI honest without
    # touching the Google or Microsoft text.
    connect_caption = (
        "You will authorize on Atlassian's own sign-in page and be returned here. "
        "The request carries a signed, single-use state value; your credentials are "
        "never seen by this app. Connecting grants read-only access to your Atlassian "
        "identity and to the Jira sites you select on the consent screen, and creates "
        "nothing in Jira."
    )

    # The shared success message otherwise tells the user to pick this provider
    # under Transcript Source, which does not apply to Jira.
    post_connect_hint = "Jira is connected. Nothing has been created in Jira."

    # --- Configuration (delegated; this class holds no credential names) ----

    def is_configured(self) -> bool:
        return jira_config.is_configured()

    def get_missing_configuration(self) -> list[str]:
        return jira_config.get_missing_configuration()

    def get_setup_instructions(self) -> str:
        """
        Configuration steps, with the scopes this ticket actually requests.

        The credential steps come from ``jira_config``; the scope requirement is
        added here because the scope list belongs to the authorization request,
        not to the environment. Variable names only -- never values.
        """
        return jira_config.get_setup_instructions() + (
            "\n\n**Scopes to enable** on the app's Permissions screen, or authorization "
            "will be rejected:\n\n"
            + "".join("- `{}`\n".format(scope) for scope in self.SCOPES)
            + "\nAll three are read-only: `read:me` shows which Atlassian account is "
            "connected, `{}` is what makes your Jira sites visible to the "
            "accessible-resources endpoint, and `offline_access` returns a refresh token. "
            "No write or manage scope is requested, because nothing in this app can "
            "change anything in Jira.".format(self.SITE_SCOPE)
        )

    # --- OAuth -------------------------------------------------------------

    def get_authorization_url(
        self,
        redirect_uri: str,
        state: Optional[str] = None,
        code_challenge: Optional[str] = None,
        code_challenge_method: str = "S256",
    ) -> str:
        """
        Build the Atlassian 3LO consent URL.

        ``code_challenge``/``code_challenge_method`` are accepted so this class
        is a drop-in for the shared connect button, but they are intentionally
        not sent: Atlassian's 3LO documentation enumerates the accepted
        authorization parameters and PKCE is not among them, so including an
        undocumented parameter risks a rejected request for no stated benefit.
        CSRF protection is unaffected -- it comes from the HMAC-signed, TTL-bound
        ``state``, which is the mechanism Atlassian's own documentation credits
        with preventing a hijacked authorization flow.
        """
        jira_config.require_configured()

        params = {
            "audience": self.AUDIENCE,
            "client_id": jira_config.get_client_id(),
            "scope": " ".join(self.SCOPES),
            "redirect_uri": redirect_uri,
        }
        if state:
            params["state"] = state
        params["response_type"] = "code"
        # offline_access only yields a refresh token when consent is shown.
        params["prompt"] = "consent"

        return "{}?{}".format(self.AUTH_URL, urllib.parse.urlencode(params))

    def _post_token_request(self, payload: dict, failure_prefix: str) -> dict[str, Any]:
        """
        Call Atlassian's token endpoint with a JSON body.

        Uses ``json=`` rather than ``data=`` because Atlassian documents this
        endpoint as ``Content-Type: application/json``, unlike Google's and
        Microsoft's form-encoded endpoints. Never logs or echoes the payload,
        which carries the client secret and the authorization code.
        """
        try:
            response = requests.post(self.TOKEN_URL, json=payload, timeout=self.TIMEOUT_SECONDS)
        except requests.RequestException as e:
            raise ProviderAuthenticationError(
                "Network error contacting Atlassian's token endpoint: {}".format(e)
            )

        try:
            data = response.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}

        if getattr(response, "status_code", 0) != 200:
            error_code = str(data.get("error", "")).strip()
            description = str(
                data.get("error_description") or error_code or "OAuth token exchange failed"
            ).strip()
            if error_code == "invalid_grant":
                description = (
                    "{} (the authorization code or refresh token is invalid, expired, already "
                    "used, or was issued for a different redirect URI; Atlassian disables a "
                    "refresh token once it has been exchanged)".format(description)
                )
            raise ProviderAuthenticationError("{}: {}".format(failure_prefix, description))

        if not str(data.get("access_token") or "").strip():
            raise ProviderAuthenticationError(
                "{}: Atlassian's response did not include an access token.".format(failure_prefix)
            )
        return data

    def exchange_code_for_token(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Exchange an authorization code for tokens.

        ``code_verifier`` is accepted for signature compatibility with the
        shared callback and not sent, for the reason given in
        ``get_authorization_url``.
        """
        jira_config.require_configured()

        payload = {
            "grant_type": "authorization_code",
            "client_id": jira_config.get_client_id(),
            "client_secret": jira_config.get_client_secret(),
            "code": code,
            "redirect_uri": redirect_uri,
        }
        return self._post_token_request(payload, "Jira authorization failed")

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """
        Trade a refresh token for a fresh access token.

        Atlassian rotates refresh tokens and disables the one just used, so the
        caller must persist the ``refresh_token`` in the response.
        """
        jira_config.require_configured()
        if not refresh_token:
            raise ProviderAuthenticationError(
                "Jira token refresh failed: no refresh token is available for this session."
            )

        payload = {
            "grant_type": "refresh_token",
            "client_id": jira_config.get_client_id(),
            "client_secret": jira_config.get_client_secret(),
            "refresh_token": refresh_token,
        }
        return self._post_token_request(payload, "Jira token refresh failed")

    def get_user_profile(self, access_token: str) -> dict[str, Any]:
        """
        Best-effort Atlassian account identity, so the UI can show who is
        connected. Returns an empty dict on any failure and never raises, since
        a missing label must not break the connected panel.
        """
        try:
            response = requests.get(
                self.ME_URL,
                headers={"Authorization": "Bearer {}".format(access_token)},
                timeout=self.TIMEOUT_SECONDS,
            )
            if getattr(response, "status_code", 0) == 200:
                data = response.json()
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    # --- Read-only Jira API access -----------------------------------------

    @staticmethod
    def _error_detail(response) -> str:
        """
        Human-readable reason from a failed Atlassian response.

        Atlassian uses ``message`` on product APIs and ``error_description`` on
        OAuth ones, so both are checked. Capped so an HTML error page cannot flood
        the UI. Only server-supplied text is returned -- never the request, which
        carries the bearer token.
        """
        try:
            payload = response.json()
        except Exception:
            payload = None

        if isinstance(payload, dict):
            for key in ("message", "error_description", "error"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:400]

        text = str(getattr(response, "text", "") or "").strip()
        return text[:400] if text else "no further detail was returned"

    def _api_get(self, url: str, access_token: str):
        """
        GET an Atlassian endpoint, mapping HTTP status onto the shared provider
        error hierarchy so ``_provider_call`` can react the same way it does for
        Google and Microsoft: 401 triggers one refresh-and-retry, 403 is reported
        as an authorization problem, and everything else surfaces as an API error.
        """
        try:
            response = requests.get(
                url,
                headers={
                    "Authorization": "Bearer {}".format(access_token),
                    "Accept": "application/json",
                },
                timeout=self.TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            raise ProviderAPIError("Network error contacting Atlassian: {}".format(e))

        status = getattr(response, "status_code", 0)
        if status == 200:
            return response

        detail = self._error_detail(response)
        if status == 401:
            raise ProviderTokenExpiredError(
                "Atlassian rejected the access token as expired or invalid: {}".format(detail)
            )
        if status == 403:
            raise ProviderConsentRequiredError(
                "Atlassian refused this request. The authorization is missing a required "
                "scope, or the Atlassian account is not permitted to see this resource. "
                "Reconnecting re-runs consent. ({})".format(detail)
            )
        if status == 429:
            raise ProviderAPIError(
                "Atlassian rate-limited this request. Wait a moment and try again. "
                "({})".format(detail),
                status_code=429,
            )
        raise ProviderAPIError(
            "Atlassian returned HTTP {}: {}".format(status, detail), status_code=status
        )

    def list_accessible_sites(self, access_token: str) -> list[JiraSite]:
        """
        The Jira Cloud sites this authorization can reach.

        Wraps ``GET /oauth/token/accessible-resources``, which Atlassian documents
        as taking no request parameters and returning a JSON array. It reports the
        sites the *token* was granted scopes on, not every site the user owns, so
        an empty list is a legitimate answer and callers must handle it.

        Read-only: this creates and changes nothing in Jira. Returns an empty list
        when the account granted no site; raises a provider error when Atlassian
        answers with a failure or with a body that is not the documented shape.
        """
        response = self._api_get(self.ACCESSIBLE_RESOURCES_URL, access_token)

        try:
            payload = response.json()
        except Exception:
            raise ProviderAPIError(
                "Atlassian's accessible-resources response was not valid JSON.",
                status_code=getattr(response, "status_code", None),
            )

        if not isinstance(payload, list):
            raise ProviderAPIError(
                "Atlassian's accessible-resources response was not the documented JSON "
                "array of sites."
            )

        sites = [
            site for site in (JiraSite.from_api(item) for item in payload) if site is not None
        ]

        # Never let a malformed body look like "you have no sites": that reads as a
        # permissions problem and would send the user off re-consenting for nothing.
        if payload and not sites:
            raise ProviderAPIError(
                "Atlassian listed {} accessible resource(s), but none carried a site id, "
                "so none can be used.".format(len(payload))
            )
        return sites
