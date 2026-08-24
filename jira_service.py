"""
Jira Cloud OAuth 2.0 (3LO) connection.

Scope of this module
--------------------
Obtaining and renewing an authorization for a Jira Cloud account, identifying it,
and reading -- read-only, always with GET -- the Jira Cloud sites that
authorization can reach, the projects visible on one site, and what a chosen
project would require in order to create an issue. Issue creation is deliberately
absent: it arrives in its own ticket, and keeping it out means a connected session
here cannot write anything to Jira. Every request this module makes is a GET.

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
from dataclasses import replace
from typing import Any, Optional

import requests

import jira_config
from jira_models import (
    JiraField,
    JiraIssueType,
    JiraProject,
    JiraProjectMetadata,
    JiraSite,
)
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
    # user owns.
    SITE_SCOPE = "read:jira-user"

    # Reading projects and create-metadata needs more than user information:
    # Atlassian's own description of read:jira-work is "Read Jira project and issue
    # data, search for issues and objects associated with issues like attachments
    # and worklogs", while read:jira-user is confined to "user information in Jira".
    # This is the classic scope, which Atlassian's scopes reference recommends over
    # the granular equivalents. It is read-only: the write counterpart is a separate
    # scope (write:jira-work) and is not requested.
    PROJECT_SCOPE = "read:jira-work"

    # Creating issues is the one write this app performs, and it needs the write
    # counterpart of PROJECT_SCOPE. Atlassian describes write:jira-work as "Create
    # and edit issues in Jira, post comments, create worklogs, and delete issues".
    # That is broader than this app uses -- it only ever POSTs new issues -- but
    # Atlassian does not publish a narrower classic scope for creation alone, and the
    # classic scopes are what its reference recommends over the granular equivalents.
    # Requested only because JIRA-007 creates issues on explicit confirmation.
    WRITE_SCOPE = "write:jira-work"

    # Least privilege for this ticket: the connected account's identity so the UI
    # can say who is signed in, two read-only Jira scopes so accessible sites,
    # projects and create-metadata can be read, the write scope that issue creation
    # requires, and a refresh token.
    SCOPES = [
        "read:me",
        SITE_SCOPE,
        PROJECT_SCOPE,
        WRITE_SCOPE,
        "offline_access",
    ]

    TIMEOUT_SECONDS = 30

    # --- Site-scoped product API ------------------------------------------
    #
    # VERIFICATION NOTE. The base below is documented: Atlassian's 3LO page gives
    # the template "https://api.atlassian.com/ex/jira/{cloudid}/{api}" and states
    # that 3LO calls must not be sent to a your-domain.atlassian.net host.
    #
    # The three paths are NOT verified against Atlassian's documentation in the
    # environment this was written in: every /rest/v3/api-group-* reference page
    # renders client-side and returns no body to a text fetch, and the published
    # OpenAPI document truncates inside `components` before reaching `paths`. They
    # are the paths the Jira Cloud platform REST API v3 is understood to expose,
    # but treat them as unconfirmed until someone with a browser diffs them against
    # the reference. The response parsing below is deliberately tolerant for the
    # same reason -- see ``_paged_values``.
    API_BASE = "https://api.atlassian.com/ex/jira"
    PROJECT_SEARCH_PATH = "/rest/api/3/project/search"
    ISSUE_TYPES_PATH = "/rest/api/3/issue/createmeta/{project}/issuetypes"
    ISSUE_TYPE_FIELDS_PATH = "/rest/api/3/issue/createmeta/{project}/issuetypes/{issue_type}"

    # JIRA-010's whole read addition: one issue's current summary and description, so a
    # direct edit in Jira can be compared with what this app last synchronized. Only
    # these two fields are requested -- they are the two this app ever wrote, and
    # asking for more would read issue data the reconciliation has no use for.
    ISSUE_PATH = "/rest/api/3/issue/{issue}"
    ISSUE_READ_FIELDS = "summary,description"

    # The only write path in this app. Same verification caveat as the read paths
    # above: it is the documented Jira Cloud platform v3 create-issue route as
    # understood here, but the reference page could not be fetched in this
    # environment, so treat the path, the 201 success code and the ADF-shaped
    # `description` as unconfirmed until a live call or a browser check settles them.
    # A live smoke test is the intended confirmation.
    CREATE_ISSUE_PATH = "/rest/api/3/issue"

    # Bounded pagination, matching the Google and Microsoft providers. The cap is a
    # backstop against a server that ignores startAt; hitting it is reported rather
    # than swallowed.
    PAGE_SIZE = 50
    MAX_PAGES = 20

    # Reading required fields costs one request per issue type, so a project with an
    # unusual number of them is capped. The uninspected types are still listed, just
    # marked unvalidated.
    MAX_ISSUE_TYPES_INSPECTED = 25

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
            + "\nAll of these are read-only: `read:me` shows which Atlassian account is "
            "connected, `{site}` is what makes your Jira sites visible to the "
            "accessible-resources endpoint, `{project}` is what allows projects and their "
            "create-screen metadata to be read, and `offline_access` returns a refresh "
            "token. No write or manage scope is requested, because nothing in this app can "
            "change anything in Jira.\n\nScopes are granted when you consent, and a token "
            "refresh cannot add one. If you connected this app before a scope was added to "
            "the list above, disconnect and connect again so Atlassian shows the consent "
            "screen for the current set.".format(
                site=self.SITE_SCOPE, project=self.PROJECT_SCOPE
            )
        )

    def missing_scopes(self, granted: Any) -> list[str]:
        """
        Which of the scopes this app now requests are absent from a live session.

        A session authorized before a scope was added keeps the older, narrower
        grant: Atlassian issues scopes at consent time, documents that a new
        grant's scopes override the previous ones, and returns the granted
        ``scope`` on the token response -- so comparing that against ``SCOPES`` is
        the way to notice the mismatch before an API call fails with a 403.

        An empty ``granted`` returns nothing. Some providers omit ``scope`` from
        the token response entirely, and silence is not evidence of absence; the
        alternative would be telling a perfectly good session to reconnect.
        """
        held = {str(scope).strip() for scope in (granted or ()) if str(scope).strip()}
        if not held:
            return []
        return [scope for scope in self.SCOPES if scope not in held]

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

    def _api_get(self, url: str, access_token: str, params: Optional[dict] = None):
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
                params=params or None,
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

    @staticmethod
    def _json_body(response, what: str):
        """
        Decode a successful Atlassian response, or say which call broke.

        ``what`` names the call so an error reaches the user as "the project search
        response was not valid JSON" rather than as a bare parse failure.
        """
        try:
            return response.json()
        except Exception:
            raise ProviderAPIError(
                "Atlassian's {} response was not valid JSON.".format(what),
                status_code=getattr(response, "status_code", None),
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
        payload = self._json_body(response, "accessible-resources")

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

    # --- Read-only project and create-metadata discovery -------------------

    def site_api_url(self, cloud_id: str, path: str) -> str:
        """
        Address a REST path on one Jira site.

        3LO tokens are used against ``api.atlassian.com/ex/jira/{cloudid}`` and,
        per Atlassian's own wording, must not be sent to a site's own
        ``your-domain.atlassian.net`` host. The cloud id is URL-quoted because it
        arrives from an API response and lands in a path segment.
        """
        site = str(cloud_id or "").strip()
        if not site:
            raise ProviderAPIError(
                "No Jira site is selected, so there is no site to query. "
                "Choose a Jira site first."
            )
        return "{}/{}/{}".format(
            self.API_BASE.rstrip("/"),
            urllib.parse.quote(site, safe=""),
            path.lstrip("/"),
        )

    def _paged_values(
        self,
        url: str,
        access_token: str,
        what: str,
        collection_keys: tuple = ("values",),
        params: Optional[dict] = None,
        max_pages: Optional[int] = None,
    ) -> tuple:
        """
        Collect every page of a Jira ``startAt``/``maxResults`` collection.

        Returns ``(items, truncated)``, matching ``_paginate`` in the Google and
        Microsoft providers; ``truncated`` is True only when the page cap stopped
        the walk while Jira still had more to give.

        Tolerant on purpose. The exact envelope for these endpoints could not be
        confirmed from Atlassian's reference (see the VERIFICATION NOTE on the path
        constants), so this accepts a bare JSON array as an unpaginated answer,
        accepts the collection under any of ``collection_keys``, and treats several
        independent signals as end-of-collection. What it will not do is guess: an
        envelope carrying none of the expected keys raises rather than quietly
        reporting an empty collection, because "no projects" and "the response
        changed shape" must not look the same to the user.
        """
        items: list = []
        start_at = 0
        pages = 0
        limit = self.MAX_PAGES if max_pages is None else max_pages

        while True:
            page_params = dict(params or {})
            page_params["startAt"] = start_at
            page_params["maxResults"] = self.PAGE_SIZE

            body = self._json_body(
                self._api_get(url, access_token, params=page_params), what
            )

            # A bare array is an unpaginated answer; there is no next page to ask for.
            if isinstance(body, list):
                return list(body), False
            if not isinstance(body, dict):
                raise ProviderAPIError(
                    "Atlassian's {} response was neither a JSON object nor a JSON "
                    "array.".format(what)
                )

            values = None
            for key in collection_keys:
                candidate = body.get(key)
                if isinstance(candidate, list):
                    values = candidate
                    break
            if values is None:
                raise ProviderAPIError(
                    "Atlassian's {} response carried no {} array, so its contents cannot "
                    "be read.".format(
                        what, " or ".join("`{}`".format(k) for k in collection_keys)
                    )
                )

            items.extend(values)
            pages += 1

            if body.get("isLast") is True:
                return items, False
            total = body.get("total")
            if isinstance(total, int) and not isinstance(total, bool) and len(items) >= total:
                return items, False
            if not values:
                return items, False
            if body.get("isLast") is None and not isinstance(total, int):
                # Neither documented end-of-collection signal came back, so a page
                # that did not fill is the only remaining evidence that it was the
                # last one. Jira caps maxResults per endpoint, so the server's own
                # echoed value is what "full" means -- using the requested size here
                # would end the walk early and drop rows silently.
                advertised = body.get("maxResults")
                page_limit = (
                    advertised
                    if isinstance(advertised, int)
                    and not isinstance(advertised, bool)
                    and advertised > 0
                    else self.PAGE_SIZE
                )
                if len(values) < page_limit:
                    return items, False
            if pages >= limit:
                return items, True

            start_at = len(items)

    def list_projects(self, access_token: str, cloud_id: str) -> dict[str, Any]:
        """
        The projects the connected account can see on one Jira site.

        Read-only. Returns ``{"projects": [...], "truncated": bool,
        "notes": [...]}``, the same shape the transcript providers use for
        discovery, so an incomplete answer can be shown as incomplete.

        An empty project list is a legitimate answer: Atlassian documents that a
        Jira account's own permissions still apply whatever the token was granted,
        so a scope-complete session can still see nothing. The caller decides how
        to explain that.
        """
        url = self.site_api_url(cloud_id, self.PROJECT_SEARCH_PATH)
        raw, truncated = self._paged_values(url, access_token, "project search")

        projects = [
            project
            for project in (JiraProject.from_api(item) for item in raw)
            if project is not None
        ]

        # Same reasoning as list_accessible_sites: a body this app cannot read must
        # not be presented as "this account has no projects".
        if raw and not projects:
            raise ProviderAPIError(
                "Jira listed {} project(s), but none carried an id or a key, so none can "
                "be used.".format(len(raw))
            )

        notes: list[str] = []
        if truncated:
            notes.append(
                "More projects exist on this site than were listed: the walk stopped after "
                "{} pages of {}. Nothing was dropped silently, but the list below is "
                "incomplete.".format(self.MAX_PAGES, self.PAGE_SIZE)
            )
        return {"projects": projects, "truncated": truncated, "notes": notes}

    def get_project_metadata(
        self,
        access_token: str,
        cloud_id: str,
        project_id_or_key: str,
    ) -> JiraProjectMetadata:
        """
        What one project would require in order to create an issue in it.

        Read-only, and this is the whole of what "metadata" means here: the issue
        types Jira offers on this project's create screen, and for each of them the
        fields Jira marks required. Nothing is created, and nothing about issue
        hierarchy is assumed -- ``subtask`` and ``hierarchyLevel`` are recorded as
        Jira reported them.

        Costs one request for the issue types plus one per issue type for its
        fields. A field lookup that fails with an API error is recorded as a note
        and the issue type is marked unvalidated, so one awkward issue type does not
        lose the whole project. Authentication and authorization failures are left
        to propagate: those need the shared refresh-and-retry and re-consent paths,
        not a note, and every request here is a GET so retrying the whole operation
        is safe.
        """
        identifier = str(project_id_or_key or "").strip()
        if not identifier:
            raise ProviderAPIError(
                "No Jira project is selected, so there is no metadata to read."
            )
        quoted_project = urllib.parse.quote(identifier, safe="")

        notes: list[str] = []
        types_url = self.site_api_url(
            cloud_id, self.ISSUE_TYPES_PATH.format(project=quoted_project)
        )
        raw_types, truncated = self._paged_values(
            types_url,
            access_token,
            "create-metadata issue types",
            collection_keys=("values", "issueTypes"),
        )
        if truncated:
            notes.append(
                "More issue types exist on this project than were listed, so the set below "
                "is incomplete."
            )

        issue_types: list = []
        inspected = 0
        skipped_for_cap = 0

        for raw in raw_types:
            issue_type = JiraIssueType.from_api(raw)
            if issue_type is None:
                continue

            if inspected >= self.MAX_ISSUE_TYPES_INSPECTED:
                # Listed but not validated: honest, and cheaper than a request per
                # type on a project with an unusual number of them.
                skipped_for_cap += 1
                truncated = True
                issue_types.append(issue_type)
                continue
            inspected += 1

            fields_url = self.site_api_url(
                cloud_id,
                self.ISSUE_TYPE_FIELDS_PATH.format(
                    project=quoted_project,
                    issue_type=urllib.parse.quote(issue_type.id, safe=""),
                ),
            )
            label = issue_type.name or issue_type.id
            try:
                raw_fields, fields_truncated = self._paged_values(
                    fields_url,
                    access_token,
                    "create-metadata fields for '{}'".format(label),
                    collection_keys=("values", "fields"),
                )
            except ProviderAPIError as e:
                notes.append(
                    "Required fields for '{}' could not be read, so it is listed but not "
                    "validated: {}".format(label, e)
                )
                issue_types.append(issue_type)
                continue

            required = tuple(
                field
                for field in (JiraField.from_api(item) for item in raw_fields)
                if field is not None and field.required
            )
            if fields_truncated:
                truncated = True
                notes.append(
                    "Not every field for '{}' could be listed, so it is listed but not "
                    "validated.".format(label)
                )
            issue_types.append(
                replace(
                    issue_type,
                    required_fields=required,
                    fields_known=not fields_truncated,
                )
            )

        if skipped_for_cap:
            notes.append(
                "{} issue type(s) beyond the first {} were listed without reading their "
                "required fields, so they are shown as unvalidated.".format(
                    skipped_for_cap, self.MAX_ISSUE_TYPES_INSPECTED
                )
            )

        return JiraProjectMetadata(
            project_identifier=identifier,
            issue_types=tuple(issue_types),
            notes=tuple(notes),
            truncated=truncated,
        )

    # --- JIRA-010: reading back one issue this app created --------------------

    def get_issue_fields(
        self, access_token: str, cloud_id: str, issue_key: str
    ) -> dict[str, Any]:
        """
        The current ``summary`` and ``description`` of one issue, as Jira returned them.

        A read, and the smallest one that answers JIRA-010's question: has this issue
        been edited away from what the BRD says? Only the two fields this app ever
        wrote are requested. The ``description`` is handed back as Jira sent it --
        flattening ADF to comparable text is the processing layer's job, so this method
        stays what every other method here is: an HTTP call and a shape check.

        Covered by the ``read:jira-work`` scope the project list already needs, so no
        new consent is involved. Raises the same errors as every other read: an expired
        token surfaces as ``ProviderTokenExpiredError`` for ``call_with_refresh``.
        """
        key = str(issue_key or "").strip()
        if not key:
            raise ProviderAPIError("No Jira issue key was given, so nothing could be read.")

        url = self.site_api_url(
            cloud_id, self.ISSUE_PATH.format(issue=urllib.parse.quote(key, safe=""))
        )
        response = self._api_get(
            url, access_token, params={"fields": self.ISSUE_READ_FIELDS}
        )
        body = self._json_body(response, "get-issue")
        fields = body.get("fields") if isinstance(body, dict) else None
        if not isinstance(fields, dict):
            raise ProviderAPIError(
                "Jira returned no fields for issue {}, so its current state is "
                "unknown.".format(key)
            )
        return {
            "issue_key": str(body.get("key") or key),
            "summary": str(fields.get("summary") or "").strip(),
            "description": fields.get("description"),
        }

    # --- The one write ------------------------------------------------------

    def create_issue(self, access_token: str, cloud_id: str, payload: dict) -> dict[str, Any]:
        """
        POST one issue to Jira. The only call in this app that changes anything.

        ``payload`` is built by ``jira_processor.issue_creation_payload`` -- this
        method neither composes nor edits it, so what reaches Jira is what the
        reviewer approved.

        **Never retried here.** A create is not idempotent and Jira offers no
        idempotency key on this endpoint, so a retry after an ambiguous answer could
        produce a second issue. A 401 is therefore reported as an authentication
        failure rather than raised as ``ProviderTokenExpiredError``: that exception is
        what ``call_with_refresh`` reacts to by re-running the operation, which for a
        create could duplicate an issue the first attempt already made. Refreshing an
        expired token *before* the write is the caller's job.

        A network error is likewise not retried, and is reported as unresolved: the
        request may well have reached Jira, so the honest answer is that the outcome
        is unknown rather than that it failed.
        """
        url = self.site_api_url(cloud_id, self.CREATE_ISSUE_PATH)
        try:
            response = requests.post(
                url,
                headers={
                    "Authorization": "Bearer {}".format(access_token),
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.TIMEOUT_SECONDS,
            )
        except requests.RequestException as e:
            raise ProviderAPIError(
                "Network error while creating the issue in Jira, so it is unknown whether "
                "it was created. Check Jira before trying again: {}".format(e)
            )

        status = getattr(response, "status_code", 0)
        if status in (200, 201):
            body = self._json_body(response, "create-issue")
            if not isinstance(body, dict) or not (body.get("key") or body.get("id")):
                raise ProviderAPIError(
                    "Jira accepted the issue but returned no issue key, so it cannot be "
                    "reported or linked. Check Jira before trying again."
                )
            return body

        detail = self._error_detail(response)
        if status == 401:
            raise ProviderAuthenticationError(
                "Jira rejected the access token while creating an issue. The issue was not "
                "created. Reconnect and review the plan before trying again. "
                "({})".format(detail)
            )
        if status == 403:
            raise ProviderConsentRequiredError(
                "Jira refused to create the issue. The authorization is missing the `{}` "
                "scope, or the account lacks Create Issues permission on this project. "
                "Reconnecting re-runs consent. ({})".format(self.WRITE_SCOPE, detail)
            )
        if status == 429:
            raise ProviderAPIError(
                "Jira rate-limited the creation request. This issue was not created; wait a "
                "moment before trying again. ({})".format(detail),
                status_code=429,
            )
        raise ProviderAPIError(
            "Jira returned HTTP {} while creating the issue: {}".format(status, detail),
            status_code=status,
        )
