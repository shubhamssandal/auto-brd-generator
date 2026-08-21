"""
Google Meet / Google Workspace transcript provider.

Everything here talks to the real Google APIs:

* OAuth 2.0 authorization code flow with PKCE (accounts.google.com)
* Google Meet REST API v2 -- conference records, transcripts, transcript entries,
  participants, and meeting spaces
* Google Drive API v3 -- plain-text export of the Google Docs transcript artifact
  that Meet writes for each transcribed conference

Credentials are read from the environment on every call and are never logged,
returned, rendered, or written to disk. Access tokens are only ever placed in an
Authorization header.
"""

import os
import re
import urllib.parse
from typing import Any, Optional

import requests

from brd_models import NormalizedTranscript
from providers.base import (
    ProviderAPIError,
    ProviderAuthenticationError,
    ProviderConsentRequiredError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderTokenExpiredError,
    TranscriptProvider,
    TranscriptUnavailableError,
)


class GoogleMeetProvider(TranscriptProvider):
    """
    Real provider adaptor for Google Meet & Google Workspace meeting transcripts.
    Reads credentials securely from environment variables.
    """

    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
    MEET_API_BASE = "https://meet.googleapis.com/v2"
    DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

    SCOPES = [
        "https://www.googleapis.com/auth/meetings.space.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
        "openid",
        "email",
    ]

    TIMEOUT_SECONDS = 20

    # Google caps pageSize at 100 for conference records, transcripts and entries.
    MAX_PAGE_SIZE = 100
    # Safety rails so a pathological response can never spin forever. Hitting one
    # of these is reported to the caller, never swallowed.
    MAX_RECORD_PAGES = 20      # up to 2,000 conference records
    MAX_TRANSCRIPT_PAGES = 10  # up to 1,000 transcripts per conference record
    MAX_ENTRY_PAGES = 200      # up to 20,000 transcript entries per transcript
    MAX_DRIVE_PAGES = 10       # up to 1,000 Drive transcript documents

    # How many conference records to probe for transcripts during discovery.
    # Each record costs one extra API call, so the default keeps the UI responsive
    # and the cap is always surfaced to the user rather than hidden.
    DEFAULT_RECORD_PROBE_LIMIT = 25

    # --- Configuration -----------------------------------------------------

    def _get_client_id(self) -> str:
        return (
            os.getenv("GOOGLE_WORKSPACE_CLIENT_ID", "").strip()
            or os.getenv("GOOGLE_CLIENT_ID", "").strip()
        )

    def _get_client_secret(self) -> str:
        return (
            os.getenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "").strip()
            or os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
        )

    @property
    def name(self) -> str:
        return "google_meet"

    @property
    def display_name(self) -> str:
        return "Google Meet"

    def is_configured(self) -> bool:
        """Checks if required OAuth credentials are set in environment."""
        return bool(self._get_client_id() and self._get_client_secret())

    def get_missing_configuration(self) -> list[str]:
        """Returns missing configuration variable names."""
        missing = []
        if not self._get_client_id():
            missing.append("GOOGLE_WORKSPACE_CLIENT_ID (or GOOGLE_CLIENT_ID)")
        if not self._get_client_secret():
            missing.append("GOOGLE_WORKSPACE_CLIENT_SECRET (or GOOGLE_CLIENT_SECRET)")
        return missing

    def get_setup_instructions(self) -> str:
        """Detailed instructions for configuring Google Meet integration."""
        return (
            "### Google Meet & Workspace Configuration Prerequisites\n\n"
            "To enable live Google Meet transcript retrieval, configure the following:\n\n"
            "1. **Google Cloud Project**: Go to Google Cloud Console and select/create a project.\n"
            "2. **Enable APIs**:\n"
            "   - Enable the **Google Meet API** (`meet.googleapis.com`).\n"
            "   - Enable the **Google Drive API** (`drive.googleapis.com`) for transcript Google Docs.\n"
            "3. **Configure OAuth 2.0 Credentials**:\n"
            "   - Create an OAuth 2.0 Client ID of type **Web Application**.\n"
            "   - Add the Authorized Redirect URI (default: `http://localhost:8501`).\n"
            "   - While the consent screen is in *Testing*, add your own Google account as a test user.\n"
            "4. **Requested scopes** (read-only):\n"
            "   - `https://www.googleapis.com/auth/meetings.space.readonly`\n"
            "   - `https://www.googleapis.com/auth/drive.readonly`\n"
            "   - `openid`, `email`\n"
            "5. **Google Workspace requirement for transcripts**:\n"
            "   - Meet **transcription** is a paid Google Workspace feature (Business Standard/Plus,\n"
            "     Enterprise, Education Plus, or a Gemini for Workspace add-on).\n"
            "   - Transcription must be enabled in the Workspace Admin console, and the meeting\n"
            "     must actually have been transcribed.\n"
            "   - Personal `@gmail.com` accounts can complete the OAuth flow but generally cannot\n"
            "     produce Meet transcripts, so discovery will legitimately return nothing.\n"
            "6. **Environment variable names** (set the values in your local `.env`, never in code):\n"
            "   - `GOOGLE_WORKSPACE_CLIENT_ID`\n"
            "   - `GOOGLE_WORKSPACE_CLIENT_SECRET`\n"
            "   - `APP_REDIRECT_URI`\n"
        )

    # --- OAuth -------------------------------------------------------------

    def get_authorization_url(
        self,
        redirect_uri: str,
        state: Optional[str] = None,
        code_challenge: Optional[str] = None,
        code_challenge_method: str = "S256",
    ) -> str:
        """Generates the Google OAuth 2.0 user consent URL."""
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                f"Google Meet is not configured. Missing: {', '.join(self.get_missing_configuration())}"
            )

        params = {
            "client_id": self._get_client_id(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.SCOPES),
            # access_type=offline + prompt=consent is what makes Google return a
            # refresh token, so a session can outlive the 1-hour access token.
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        if state:
            params["state"] = state
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = code_challenge_method

        return f"{self.AUTH_URL}?{urllib.parse.urlencode(params)}"

    def _post_token_request(self, payload: dict, failure_prefix: str) -> dict[str, Any]:
        """Shared token endpoint call. Never logs or echoes the payload."""
        try:
            response = requests.post(self.TOKEN_URL, data=payload, timeout=self.TIMEOUT_SECONDS)
        except requests.RequestException as e:
            raise ProviderAuthenticationError(f"Network error contacting Google's token endpoint: {e}")

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
                    f"{description} (the authorization code or refresh token is invalid, expired, "
                    "already used, or was issued for a different redirect URI)"
                )
            raise ProviderAuthenticationError(f"{failure_prefix}: {description}")

        if not str(data.get("access_token") or "").strip():
            raise ProviderAuthenticationError(
                f"{failure_prefix}: Google's response did not include an access token."
            )
        return data

    def exchange_code_for_token(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: Optional[str] = None,
    ) -> dict[str, Any]:
        """Exchanges authorization code for access and refresh tokens."""
        if not self.is_configured():
            raise ProviderNotConfiguredError("Google Meet provider is not configured.")

        payload = {
            "client_id": self._get_client_id(),
            "client_secret": self._get_client_secret(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier

        return self._post_token_request(payload, "Google OAuth failed")

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Refreshes an expired access token using the refresh token."""
        if not self.is_configured():
            raise ProviderNotConfiguredError("Google Meet provider is not configured.")
        if not refresh_token:
            raise ProviderAuthenticationError(
                "Google token refresh failed: no refresh token is available for this session."
            )

        payload = {
            "client_id": self._get_client_id(),
            "client_secret": self._get_client_secret(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        return self._post_token_request(payload, "Google token refresh failed")

    def get_user_profile(self, access_token: str) -> dict[str, Any]:
        """Retrieves authenticated user profile information (email, name)."""
        try:
            res = requests.get(
                self.USERINFO_URL,
                headers=self._auth_headers(access_token),
                timeout=self.TIMEOUT_SECONDS,
            )
            if getattr(res, "status_code", 0) == 200:
                data = res.json()
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    # --- HTTP plumbing -----------------------------------------------------

    @staticmethod
    def _auth_headers(access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    @staticmethod
    def _error_detail(response) -> str:
        """Extract a human-readable, secret-free error message from a response."""
        try:
            body = response.json()
        except Exception:
            body = None

        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("status") or ""
                if message:
                    return str(message)[:400]
            elif isinstance(error, str):
                return str(body.get("error_description") or error)[:400]

        text = getattr(response, "text", "") or ""
        return str(text).strip()[:400] or "no further detail was returned"

    def _api_get(
        self,
        url: str,
        access_token: str,
        params: Optional[dict] = None,
        extra_headers: Optional[dict] = None,
    ):
        """
        GET a Google API endpoint, translating HTTP failures into typed errors.

        401 becomes ProviderTokenExpiredError so callers can refresh and retry once;
        403 becomes ProviderConsentRequiredError because no retry will fix a missing
        scope or a disabled API.
        """
        headers = self._auth_headers(access_token)
        if extra_headers:
            headers.update(extra_headers)

        try:
            response = requests.get(url, headers=headers, params=params, timeout=self.TIMEOUT_SECONDS)
        except requests.RequestException as e:
            raise ProviderAPIError(f"Network error contacting Google: {e}")

        status = getattr(response, "status_code", 0)
        if 200 <= status < 300:
            return response

        detail = self._error_detail(response)

        if status == 401:
            raise ProviderTokenExpiredError(
                f"The Google access token was rejected as expired or invalid: {detail}"
            )
        if status == 403:
            raise ProviderConsentRequiredError(
                "Google refused this request as unauthorized (HTTP 403): "
                f"{detail}. Confirm the Google Meet and Google Drive APIs are enabled for the "
                "project, that the granted scopes cover this call, and that your Workspace "
                "policy permits transcript access."
            )
        if status == 404:
            raise ProviderAPIError(
                f"Google could not find the requested resource (HTTP 404): {detail}", status_code=404
            )
        if status == 429:
            raise ProviderAPIError(
                f"Google rate-limited this request (HTTP 429): {detail}. Try again shortly.",
                status_code=429,
            )
        raise ProviderAPIError(f"Google API error (HTTP {status}): {detail}", status_code=status)

    @staticmethod
    def _json_body(response) -> dict:
        try:
            body = response.json()
        except Exception:
            raise ProviderAPIError("Google returned a response that could not be parsed as JSON.")
        return body if isinstance(body, dict) else {}

    def _paginate(
        self,
        url: str,
        access_token: str,
        items_key: str,
        params: Optional[dict] = None,
        max_pages: int = 10,
    ) -> tuple:
        """
        Walk every page of a Google list endpoint.

        Returns ``(items, truncated)``. ``truncated`` is True only when the page cap
        was reached while more pages remained -- callers surface that to the user so
        a long meeting is never quietly cut short.
        """
        items: list = []
        page_token = None
        pages = 0

        while True:
            page_params = dict(params or {})
            page_params["pageSize"] = self.MAX_PAGE_SIZE
            if page_token:
                page_params["pageToken"] = page_token

            body = self._json_body(self._api_get(url, access_token, params=page_params))
            page_items = body.get(items_key) or []
            if isinstance(page_items, list):
                items.extend(page_items)

            page_token = body.get("nextPageToken") or None
            pages += 1

            if not page_token:
                return items, False
            if pages >= max_pages:
                return items, True

    def _require_session(self, access_token: Optional[str], action: str) -> str:
        if not self.is_configured():
            raise ProviderNotConfiguredError("Google Meet provider is not configured.")
        if not access_token:
            raise ProviderAuthenticationError(f"An active access token is required to {action}.")
        return access_token

    # --- Discovery ---------------------------------------------------------

    def _get_space_meeting_code(self, space_name: str, access_token: str, cache: dict) -> Optional[str]:
        """
        Best-effort lookup of a space's human-usable meeting code.

        The Meet API does not expose a meeting subject or title anywhere, so the
        meeting code is the only real, non-invented label available for a
        conference. Failure here is not fatal -- the entry simply stays unlabelled.
        """
        if not space_name:
            return None
        if space_name in cache:
            return cache[space_name]

        code = None
        try:
            body = self._json_body(self._api_get(f"{self.MEET_API_BASE}/{space_name}", access_token))
            code = str(body.get("meetingCode") or "").strip() or None
        except ProviderTokenExpiredError:
            raise
        except Exception:
            code = None

        cache[space_name] = code
        return code

    def discover_transcripts(
        self,
        access_token: Optional[str] = None,
        record_probe_limit: Optional[int] = None,
        include_drive_documents: bool = True,
        since_iso: Optional[str] = None,
        **kwargs,
    ) -> dict[str, Any]:
        """
        Find every Meet transcript this account can actually reach.

        Two real sources are combined:

        1. Meet API v2 conference records -> transcripts. This is authoritative and
           tells us whether the transcript file has been generated yet.
        2. Google Drive transcript documents. Meet writes a Google Doc for each
           transcribed conference; this path stays because it is the only way to
           reach transcripts of conferences that fall outside the Meet API's
           conference-record window, and because it carries the real meeting title.

        Returns ``{"transcripts": [...], "truncated": bool, "notes": [...]}``.
        Nothing is invented: unknown titles stay None and only transcripts the API
        reported are listed.
        """
        token = self._require_session(access_token, "list transcripts")
        probe_limit = self.DEFAULT_RECORD_PROBE_LIMIT if record_probe_limit is None else record_probe_limit

        transcripts: list[dict[str, Any]] = []
        notes: list[str] = []
        truncated = False
        seen_document_ids: set = set()
        space_cache: dict = {}

        # --- 1. Meet API v2 conference records
        record_params = {}
        if since_iso:
            record_params["filter"] = f'start_time>="{since_iso}"'

        meet_api_reachable = True
        try:
            records, records_truncated = self._paginate(
                f"{self.MEET_API_BASE}/conferenceRecords",
                token,
                "conferenceRecords",
                params=record_params or None,
                max_pages=self.MAX_RECORD_PAGES,
            )
        except ProviderTokenExpiredError:
            raise
        except ProviderConsentRequiredError as e:
            meet_api_reachable = False
            records, records_truncated = [], False
            notes.append(f"Google Meet API conference records are not accessible: {e}")
        except ProviderError as e:
            meet_api_reachable = False
            records, records_truncated = [], False
            notes.append(f"Could not list Google Meet conference records: {e}")

        if records_truncated:
            truncated = True
            notes.append(
                "More Google Meet conference records exist than were retrieved; "
                "narrow the search window to see older meetings."
            )

        if meet_api_reachable and not records:
            notes.append("The Google Meet API returned no conference records for this account.")

        if len(records) > probe_limit:
            truncated = True
            notes.append(
                f"Checked the {probe_limit} most recent of {len(records)} conference records for "
                "transcripts. Older records were not probed."
            )

        for record in records[:probe_limit]:
            if not isinstance(record, dict):
                continue
            record_name = str(record.get("name") or "").strip()
            if not record_name:
                continue

            try:
                record_transcripts, record_truncated = self._paginate(
                    f"{self.MEET_API_BASE}/{record_name}/transcripts",
                    token,
                    "transcripts",
                    max_pages=self.MAX_TRANSCRIPT_PAGES,
                )
            except ProviderTokenExpiredError:
                raise
            except ProviderError as e:
                notes.append(f"Could not read transcripts for {record_name}: {e}")
                continue

            if record_truncated:
                truncated = True
                notes.append(f"Not all transcripts for {record_name} could be listed.")

            if not record_transcripts:
                continue

            space_name = str(record.get("space") or "").strip()
            meeting_code = self._get_space_meeting_code(space_name, token, space_cache)
            record_start = record.get("startTime") or None

            for entry in record_transcripts:
                if not isinstance(entry, dict):
                    continue
                transcript_name = str(entry.get("name") or "").strip()
                if not transcript_name:
                    continue

                state = str(entry.get("state") or "STATE_UNSPECIFIED")
                docs_destination = entry.get("docsDestination") or {}
                document_id = str(docs_destination.get("document") or "").strip() or None
                if document_id:
                    seen_document_ids.add(document_id)

                started = entry.get("startTime") or record_start
                label_date = str(started)[:16].replace("T", " ") if started else "unknown time"
                label_code = meeting_code or "meeting code unavailable"
                available = state in ("ENDED", "FILE_GENERATED")

                transcripts.append(
                    {
                        "id": transcript_name,
                        # The Meet API exposes no meeting subject, so there is no
                        # honest title to report here.
                        "title": None,
                        "date": started,
                        "display_label": "Google Meet · {} · {}{}".format(
                            label_code,
                            label_date,
                            "" if available else " (transcript not ready)",
                        ),
                        "source_type": "meet_api",
                        "participants": [],
                        "available": available,
                        "details": {
                            "conference_record": record_name,
                            "meeting_code": meeting_code,
                            "space": space_name or None,
                            "transcript_state": state,
                            "transcript_start_time": entry.get("startTime"),
                            "transcript_end_time": entry.get("endTime"),
                            "drive_document_id": document_id,
                        },
                    }
                )

        # --- 2. Google Drive transcript documents
        if include_drive_documents:
            query = (
                "mimeType='application/vnd.google-apps.document' and "
                "(name contains 'Transcript' or name contains 'transcript') and trashed=false"
            )
            try:
                files, files_truncated = self._paginate(
                    f"{self.DRIVE_API_BASE}/files",
                    token,
                    "files",
                    params={
                        "q": query,
                        "fields": "nextPageToken, files(id, name, createdTime, modifiedTime)",
                        "orderBy": "createdTime desc",
                    },
                    max_pages=self.MAX_DRIVE_PAGES,
                )
            except ProviderTokenExpiredError:
                raise
            except ProviderError as e:
                files, files_truncated = [], False
                notes.append(f"Could not search Google Drive for transcript documents: {e}")

            if files_truncated:
                truncated = True
                notes.append("More Google Drive transcript documents exist than were retrieved.")

            for f in files:
                if not isinstance(f, dict):
                    continue
                file_id = str(f.get("id") or "").strip()
                if not file_id or file_id in seen_document_ids:
                    # Already represented by its Meet API transcript entry.
                    continue
                created = f.get("createdTime") or f.get("modifiedTime") or None
                doc_name = f.get("name") or None
                transcripts.append(
                    {
                        "id": file_id,
                        # The Drive document name is a genuine, user-visible title.
                        "title": doc_name,
                        "date": created,
                        "display_label": "Google Drive doc · {}{}".format(
                            doc_name or file_id,
                            " · " + str(created)[:10] if created else "",
                        ),
                        "source_type": "drive_doc",
                        "participants": [],
                        "available": True,
                        "details": {"drive_document_id": file_id, "created_time": created},
                    }
                )

        transcripts.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
        return {"transcripts": transcripts, "truncated": truncated, "notes": notes}

    def list_available_transcripts(self, access_token: Optional[str] = None) -> list[dict[str, Any]]:
        """Lists available meeting transcripts from Google Meet and Google Drive."""
        return self.discover_transcripts(access_token=access_token)["transcripts"]

    # --- Retrieval ---------------------------------------------------------

    def get_transcript(self, meeting_id: str, access_token: Optional[str] = None) -> NormalizedTranscript:
        """Retrieves and normalizes a transcript from Google Meet API or Google Drive."""
        token = self._require_session(access_token, "retrieve transcripts")

        identifier = str(meeting_id or "").strip()
        if not identifier:
            raise ProviderError("No Google Meet transcript or document identifier was provided.")

        if identifier.startswith("conferenceRecords/"):
            if "/transcripts/" in identifier:
                return self._transcript_from_meet_entries(identifier, token)
            return self._transcript_from_conference_record(identifier, token)

        return self._transcript_from_drive_document(identifier, token)

    def _transcript_from_conference_record(self, record_name: str, token: str) -> NormalizedTranscript:
        """Resolve a conference record to its usable transcript, then fetch it."""
        transcripts, _ = self._paginate(
            f"{self.MEET_API_BASE}/{record_name}/transcripts",
            token,
            "transcripts",
            max_pages=self.MAX_TRANSCRIPT_PAGES,
        )
        if not transcripts:
            raise TranscriptUnavailableError(
                f"Google Meet reports no transcript for {record_name}. The meeting was most likely "
                "never transcribed (transcription is a paid Google Workspace feature and must be "
                "switched on for the meeting)."
            )

        # Prefer a transcript whose file has been generated.
        ordered = sorted(
            [t for t in transcripts if isinstance(t, dict)],
            key=lambda t: 0 if str(t.get("state")) == "FILE_GENERATED" else 1,
        )
        chosen = ordered[0]
        transcript_name = str(chosen.get("name") or "").strip()
        if not transcript_name:
            raise TranscriptUnavailableError(
                f"Google Meet returned a transcript for {record_name} without a resource name."
            )
        return self._transcript_from_meet_entries(transcript_name, token)

    def _fetch_transcript_resource(self, transcript_name: str, token: str) -> dict:
        """Best-effort read of the transcript resource for state and timing."""
        try:
            return self._json_body(self._api_get(f"{self.MEET_API_BASE}/{transcript_name}", token))
        except ProviderTokenExpiredError:
            raise
        except Exception:
            return {}

    def _resolve_participant_names(self, participant_resources: list, token: str) -> dict:
        """
        Map Meet participant resource names to real display names.

        ``TranscriptEntry.participant`` is a resource name such as
        ``conferenceRecords/x/participants/y`` -- unusable as a speaker label. Each
        participant resource carries the actual display name for a signed-in,
        anonymous, or phone participant. Names that cannot be resolved are left
        out so the caller can fall back to a neutral placeholder rather than
        attributing speech to an invented person.
        """
        resolved: dict = {}
        for resource in participant_resources:
            if not resource or resource in resolved:
                continue
            try:
                body = self._json_body(self._api_get(f"{self.MEET_API_BASE}/{resource}", token))
            except ProviderTokenExpiredError:
                raise
            except Exception:
                continue

            display_name = None
            for key in ("signedinUser", "anonymousUser", "phoneUser"):
                info = body.get(key)
                if isinstance(info, dict):
                    candidate = str(info.get("displayName") or "").strip()
                    if candidate:
                        display_name = candidate
                        break
            if display_name:
                resolved[resource] = display_name
        return resolved

    def _transcript_from_meet_entries(self, transcript_name: str, token: str) -> NormalizedTranscript:
        """Build a transcript from Meet API transcript entries, fully paginated."""
        resource = self._fetch_transcript_resource(transcript_name, token)
        state = str(resource.get("state") or "") or None
        docs_destination = resource.get("docsDestination") or {}
        document_id = str(docs_destination.get("document") or "").strip() or None

        entries, entries_truncated = self._paginate(
            f"{self.MEET_API_BASE}/{transcript_name}/entries",
            token,
            "transcriptEntries",
            max_pages=self.MAX_ENTRY_PAGES,
        )

        if not entries:
            # The structured entries can be absent while the Google Docs artifact
            # exists. That artifact is the only remaining real source, so use it.
            if document_id:
                return self._transcript_from_drive_document(
                    document_id,
                    token,
                    extra_metadata={
                        "meet_transcript_name": transcript_name,
                        "transcript_state": state,
                        "retrieved_via": "drive_export_fallback",
                    },
                )
            if state == "STARTED":
                raise TranscriptUnavailableError(
                    "This Google Meet transcript is still being recorded. Try again after the "
                    "meeting ends."
                )
            if state == "ENDED":
                raise TranscriptUnavailableError(
                    "Google Meet has not finished generating this transcript yet (state: ENDED). "
                    "Try again in a few minutes."
                )
            raise TranscriptUnavailableError(
                "Google Meet returned no transcript entries for this transcript"
                + (f" (state: {state})." if state else ".")
            )

        participant_order: list = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            resource_name = str(entry.get("participant") or "").strip()
            if resource_name and resource_name not in participant_order:
                participant_order.append(resource_name)

        resolved_names = self._resolve_participant_names(participant_order, token)

        # Neutral, clearly-synthetic placeholders for participants Google would not
        # name. These are labels, not claimed identities.
        placeholders: dict = {}
        for index, resource_name in enumerate(participant_order, start=1):
            if resource_name not in resolved_names:
                placeholders[resource_name] = f"Speaker {index}"

        lines: list = []
        last_speaker = None
        languages: set = set()

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue

            resource_name = str(entry.get("participant") or "").strip()
            speaker = resolved_names.get(resource_name) or placeholders.get(resource_name) or "Unattributed speaker"

            language = str(entry.get("languageCode") or "").strip()
            if language:
                languages.add(language)

            # Meet emits many short entries per speaker; joining consecutive ones
            # keeps the transcript readable without altering any wording.
            if speaker == last_speaker and lines:
                lines[-1] = f"{lines[-1]} {text}"
            else:
                lines.append(f"{speaker}: {text}")
                last_speaker = speaker

        full_text = "\n".join(lines).strip()
        if not full_text:
            raise TranscriptUnavailableError(
                "The Google Meet transcript entries contained no spoken text."
            )

        participants = [resolved_names[r] for r in participant_order if r in resolved_names]
        conference_record = transcript_name.split("/transcripts/")[0]

        metadata: dict[str, Any] = {
            "meeting_id": transcript_name,
            "conference_record": conference_record,
            "transcript_state": state,
            "entry_count": len(entries),
            "line_count": len(lines),
            "char_count": len(full_text),
            "unresolved_participant_count": len(placeholders),
            "drive_document_id": document_id,
            "retrieved_via": "meet_api_entries",
        }
        if languages:
            metadata["language_codes"] = sorted(languages)
        if entries_truncated:
            metadata["entries_truncated"] = True
            metadata["truncation_warning"] = (
                "Google returned more transcript entries than could be retrieved in one pass; "
                "this transcript is incomplete."
            )

        return NormalizedTranscript(
            raw_text=full_text,
            source="google_meet",
            provider=self.name,
            transcript_id=transcript_name,
            # Meet exposes no meeting subject, so there is nothing truthful to put here.
            meeting_title=None,
            meeting_date=resource.get("startTime") or None,
            participants=participants,
            metadata=metadata,
        )

    def _transcript_from_drive_document(
        self,
        file_id: str,
        token: str,
        extra_metadata: Optional[dict] = None,
    ) -> NormalizedTranscript:
        """Export a Google Docs transcript artifact as plain text."""
        try:
            response = self._api_get(
                f"{self.DRIVE_API_BASE}/files/{file_id}/export",
                token,
                params={"mimeType": "text/plain"},
            )
        except ProviderConsentRequiredError as e:
            raise ProviderConsentRequiredError(
                f"Google Drive refused to export document {file_id}: {e}"
            )
        except ProviderAPIError as e:
            if e.status_code == 404:
                raise TranscriptUnavailableError(
                    f"Google Drive has no document with id {file_id}, or this account cannot see it."
                )
            raise

        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            raise TranscriptUnavailableError(
                f"Google Drive returned an empty transcript document for {file_id}."
            )

        # Best effort: the document name is the only real meeting title Google
        # gives us for this path. A failure here must not break retrieval.
        doc_name = None
        created_time = None
        try:
            info = self._json_body(
                self._api_get(
                    f"{self.DRIVE_API_BASE}/files/{file_id}",
                    token,
                    params={"fields": "id,name,createdTime,modifiedTime"},
                )
            )
            doc_name = str(info.get("name") or "").strip() or None
            created_time = info.get("createdTime") or info.get("modifiedTime") or None
        except Exception:
            doc_name = None
            created_time = None

        metadata: dict[str, Any] = {
            "file_id": file_id,
            "char_count": len(text),
            "line_count": len(text.splitlines()),
            "retrieved_via": "drive_export",
        }
        if created_time:
            metadata["created_time"] = created_time
        if extra_metadata:
            metadata.update(extra_metadata)

        return NormalizedTranscript(
            raw_text=text,
            source="google_meet",
            provider=self.name,
            transcript_id=file_id,
            meeting_title=doc_name,
            meeting_date=created_time,
            participants=self._extract_participants_from_text(text),
            metadata=metadata,
        )

    @staticmethod
    def _extract_participants_from_text(text: str) -> list[str]:
        """
        Read participant names out of an exported Meet transcript document.

        Only names the document actually states are returned: either from the
        "Attendees" block Meet writes, or from speaker labels at the start of
        lines. Roles are never inferred.
        """
        lines = text.splitlines()
        participants: list[str] = []

        for index, line in enumerate(lines):
            if line.strip().lower().rstrip(":") != "attendees":
                continue
            for candidate_line in lines[index + 1: index + 8]:
                stripped = candidate_line.strip()
                if not stripped or stripped.lower().startswith("transcript"):
                    break
                for part in stripped.split(","):
                    candidate = part.strip()
                    if candidate and len(candidate) <= 60 and candidate not in participants:
                        participants.append(candidate)
            break

        if participants:
            return participants

        excluded = {"transcript", "attendees", "meeting", "date", "note", "notes", "summary"}
        for line in lines:
            match = re.match(r"^([^:]{1,60}):\s+\S", line)
            if not match:
                continue
            candidate = match.group(1).strip()
            # Drop a trailing timestamp such as "Priya [00:01:12]".
            candidate = re.sub(r"\s*[\[(]\d{1,2}:\d{2}(?::\d{2})?[\])]\s*$", "", candidate).strip()
            if not candidate or candidate in participants:
                continue
            if len(candidate.split()) > 5:
                continue
            if any(ch in candidate for ch in ".!?"):
                continue
            if candidate.lower() in excluded:
                continue
            participants.append(candidate)

        return participants
