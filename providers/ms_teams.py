"""
Microsoft Teams transcript provider (Microsoft Graph v1.0).

Real endpoints only:

* Microsoft Entra ID v2.0 authorization-code flow with PKCE
* ``GET /me/events`` for meeting discovery -- Graph v1.0 has **no** "list all my
  online meetings" endpoint for delegated callers, so the calendar is the only
  supported way to enumerate meetings
* ``GET /me/onlineMeetings?$filter=JoinWebUrl eq '{joinUrl}'`` to turn a calendar
  event into an onlineMeeting resource
* ``GET /me/onlineMeetings/{id}/transcripts`` and ``.../transcripts/{id}/content``
  with explicit VTT / plain-text content negotiation

Credentials come from the environment on every call. Tokens are only ever placed
in an Authorization header -- never logged, echoed, or rendered.
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
    TranscriptParseError,
    TranscriptProvider,
    TranscriptUnavailableError,
)


class MSTeamsProvider(TranscriptProvider):
    """
    Real provider adaptor for Microsoft Teams meeting transcripts via Microsoft Graph API.
    Reads credentials securely from environment variables.
    """

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    # Least-privileged delegated scopes for what this app actually does: read the
    # signed-in user's calendar to find meetings, read those online meetings, and
    # read their transcripts. No write scope is requested anywhere.
    #
    # OnlineMeetingTranscript.Read.All is the *only* delegated permission Graph
    # offers for transcripts -- there is no narrower alternative -- and it requires
    # tenant administrator consent. Delegated and application permissions are not
    # interchangeable here: the application-permission variant additionally needs a
    # tenant application access policy, so this provider stays delegated-only.
    DEFAULT_SCOPES = [
        "https://graph.microsoft.com/OnlineMeetings.Read",
        "https://graph.microsoft.com/OnlineMeetingTranscript.Read.All",
        "https://graph.microsoft.com/Calendars.Read",
        "https://graph.microsoft.com/User.Read",
        "offline_access",
    ]

    # Content types Graph supports for transcript content.
    VTT_CONTENT_TYPE = "text/vtt"
    PLAIN_CONTENT_TYPE = "application/vnd.microsoft.graph.transcript+text"

    TIMEOUT_SECONDS = 20

    MAX_PAGES = 20            # bounded pagination for any @odata.nextLink chain
    EVENTS_PAGE_SIZE = 50
    # Each calendar event costs two extra Graph calls (resolve meeting, list
    # transcripts), so only the most recent events are probed. The cap is always
    # reported back to the caller instead of being hidden.
    DEFAULT_EVENT_PROBE_LIMIT = 10

    # --- Configuration -----------------------------------------------------

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
        return bool(
            self._get_client_id()
            and self._get_client_secret()
            and os.getenv("AZURE_TENANT_ID", "").strip()
        )

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
            "1. **Azure App Registration**: In Microsoft Entra ID, register a new application\n"
            "   (Web platform) and add the redirect URI `http://localhost:8501`.\n"
            "2. **Delegated Microsoft Graph API permissions** (no application permissions needed):\n"
            "   - `OnlineMeetings.Read` - read the signed-in user's online meetings\n"
            "   - `OnlineMeetingTranscript.Read.All` - read meeting transcripts\n"
            "   - `Calendars.Read` - discover which meetings exist (Graph has no\n"
            "     'list all online meetings' endpoint for delegated callers)\n"
            "   - `User.Read` - show which account is connected\n"
            "3. **Admin Consent**: A Microsoft 365 tenant administrator must grant admin consent\n"
            "   for `OnlineMeetingTranscript.Read.All`. Without it every transcript call returns\n"
            "   HTTP 403 and no retry or code change can work around it.\n"
            "4. **Teams policy**: Transcription must be enabled in the Teams admin center, and the\n"
            "   meeting must actually have been transcribed. Graph transcript access can also be\n"
            "   switched off tenant-wide, which surfaces as `GraphAccessToTranscriptsDisabled`.\n"
            "5. **Known Graph limitations** (not bugs in this app):\n"
            "   - Transcripts are unavailable for meetings that have expired.\n"
            "   - Meetings created through the Graph create-onlineMeeting API without a calendar\n"
            "     event cannot be listed; use the manual join-URL option for those.\n"
            "6. **Environment variable names** (set the values in your local `.env`, never in code):\n"
            "   - `AZURE_CLIENT_ID`\n"
            "   - `AZURE_TENANT_ID`\n"
            "   - `AZURE_CLIENT_SECRET`\n"
            "   - `APP_REDIRECT_URI`\n"
        )

    # --- OAuth -------------------------------------------------------------

    def _authority(self) -> str:
        return f"https://login.microsoftonline.com/{self._get_tenant_id()}/oauth2/v2.0"

    def get_authorization_url(
        self,
        redirect_uri: str,
        state: Optional[str] = None,
        code_challenge: Optional[str] = None,
        code_challenge_method: str = "S256",
    ) -> str:
        """Generates the Microsoft Entra ID OAuth 2.0 authorization URL."""
        if not self.is_configured():
            raise ProviderNotConfiguredError(
                f"Microsoft Teams is not configured. Missing: {', '.join(self.get_missing_configuration())}"
            )

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
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = code_challenge_method

        return f"{self._authority()}/authorize?{urllib.parse.urlencode(params)}"

    def _post_token_request(self, payload: dict, failure_prefix: str) -> dict[str, Any]:
        """Shared token endpoint call. Never logs or echoes the payload."""
        try:
            response = requests.post(
                f"{self._authority()}/token", data=payload, timeout=self.TIMEOUT_SECONDS
            )
        except requests.RequestException as e:
            raise ProviderAuthenticationError(f"Network error contacting Microsoft Entra ID: {e}")

        try:
            data = response.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}

        if getattr(response, "status_code", 0) != 200:
            description = str(
                data.get("error_description")
                or data.get("error")
                or "Microsoft token request failed"
            ).strip()
            # AADSTS65001 is the specific "user or admin has not consented" code.
            if "AADSTS65001" in description:
                description = (
                    f"{description} The requested permissions have not been consented to; "
                    "a tenant administrator must grant admin consent."
                )
            raise ProviderAuthenticationError(f"{failure_prefix}: {description}")

        if not str(data.get("access_token") or "").strip():
            raise ProviderAuthenticationError(
                f"{failure_prefix}: Microsoft's response did not include an access token."
            )
        return data

    def exchange_code_for_token(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: Optional[str] = None,
    ) -> dict[str, Any]:
        """Exchanges authorization code for Microsoft Graph access tokens."""
        if not self.is_configured():
            raise ProviderNotConfiguredError("Microsoft Teams provider is not configured.")

        payload = {
            "client_id": self._get_client_id(),
            "client_secret": self._get_client_secret(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.DEFAULT_SCOPES),
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier

        return self._post_token_request(payload, "Microsoft OAuth failed")

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Refreshes an expired Microsoft Graph access token."""
        if not self.is_configured():
            raise ProviderNotConfiguredError("Microsoft Teams provider is not configured.")
        if not refresh_token:
            raise ProviderAuthenticationError(
                "Microsoft token refresh failed: no refresh token is available for this session."
            )

        payload = {
            "client_id": self._get_client_id(),
            "client_secret": self._get_client_secret(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": " ".join(self.DEFAULT_SCOPES),
        }

        return self._post_token_request(payload, "Microsoft token refresh failed")

    def get_user_profile(self, access_token: str) -> dict[str, Any]:
        """Best-effort identity of the connected Microsoft 365 account."""
        try:
            res = requests.get(
                f"{self.GRAPH_BASE}/me",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"$select": "displayName,mail,userPrincipalName"},
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
    def _error_info(response) -> tuple:
        """
        Pull ``(codes, message)`` out of a Graph error body.

        Graph puts the actionable reason in ``error.code`` and often a more specific
        one in ``error.innerError.code`` -- for example ``SpeakerAttributionNotAllowed``
        or ``GraphAccessToTranscriptsDisabled``.
        """
        codes: list = []
        message = ""
        try:
            body = response.json()
        except Exception:
            body = None

        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or "").strip()
                if code:
                    codes.append(code)
                message = str(error.get("message") or "").strip()
                inner = error.get("innerError") or error.get("innererror")
                if isinstance(inner, dict):
                    inner_code = str(inner.get("code") or "").strip()
                    if inner_code:
                        codes.append(inner_code)
            elif isinstance(error, str):
                codes.append(error)
                message = str(body.get("error_description") or "").strip()

        if not message:
            message = str(getattr(response, "text", "") or "").strip()

        return codes, message[:400] or "no further detail was returned"

    def _graph_get(
        self,
        url: str,
        access_token: str,
        params: Optional[dict] = None,
        extra_headers: Optional[dict] = None,
    ):
        """
        GET a Graph endpoint, translating HTTP failures into typed errors.

        401 becomes ProviderTokenExpiredError so the caller can refresh once and
        retry. 403 becomes ProviderConsentRequiredError (carrying the Graph error
        code so callers can react to ``SpeakerAttributionNotAllowed``), except for
        ``GraphAccessToTranscriptsDisabled``, which is a tenant policy decision
        with no request-side workaround.
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        if extra_headers:
            headers.update(extra_headers)

        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=self.TIMEOUT_SECONDS
            )
        except requests.RequestException as e:
            raise ProviderAPIError(f"Network error contacting Microsoft Graph: {e}")

        status = getattr(response, "status_code", 0)
        if 200 <= status < 300:
            return response

        codes, message = self._error_info(response)
        code_text = " ".join(codes)

        if status == 401:
            raise ProviderTokenExpiredError(
                f"Microsoft Graph rejected the access token as expired or invalid: {message}"
            )

        if status == 403:
            if "GraphAccessToTranscriptsDisabled" in code_text:
                raise TranscriptUnavailableError(
                    "This Microsoft 365 tenant has disabled Microsoft Graph access to Teams "
                    "transcripts (GraphAccessToTranscriptsDisabled). Only a tenant administrator "
                    "can change that policy; there is no application-side workaround."
                )
            error = ProviderConsentRequiredError(
                "Microsoft Graph refused this request as unauthorized (HTTP 403"
                + (f", {code_text}" if code_text else "")
                + f"): {message}. This normally means the required delegated permissions are not "
                "granted, or that a tenant administrator has not given admin consent for "
                "OnlineMeetingTranscript.Read.All."
            )
            error.graph_code = code_text
            raise error

        if status == 404:
            raise ProviderAPIError(
                f"Microsoft Graph could not find the requested resource (HTTP 404): {message}",
                status_code=404,
            )
        if status == 429:
            raise ProviderAPIError(
                f"Microsoft Graph rate-limited this request (HTTP 429): {message}. "
                "Try again shortly.",
                status_code=429,
            )
        raise ProviderAPIError(f"Microsoft Graph error (HTTP {status}): {message}", status_code=status)

    @staticmethod
    def _json_body(response) -> dict:
        try:
            body = response.json()
        except Exception:
            raise ProviderAPIError(
                "Microsoft Graph returned a response that could not be parsed as JSON."
            )
        return body if isinstance(body, dict) else {}

    def _paginate(
        self,
        url: str,
        access_token: str,
        params: Optional[dict] = None,
        max_pages: int = MAX_PAGES,
    ) -> tuple:
        """
        Follow every ``@odata.nextLink`` page.

        Returns ``(items, truncated)``; ``truncated`` is True only if the page cap
        stopped us while Graph still had more to give.
        """
        items: list = []
        next_url = url
        next_params = dict(params or {})
        pages = 0

        while True:
            body = self._json_body(self._graph_get(next_url, access_token, params=next_params or None))
            page_items = body.get("value")
            if isinstance(page_items, list):
                items.extend(page_items)

            next_link = body.get("@odata.nextLink") or None
            pages += 1

            if not next_link:
                return items, False
            if pages >= max_pages:
                return items, True

            # nextLink is an absolute URL that already carries the query string.
            next_url = next_link
            next_params = {}

    def _require_session(self, access_token: Optional[str], action: str) -> str:
        if not self.is_configured():
            raise ProviderNotConfiguredError("Microsoft Teams provider is not configured.")
        if not access_token:
            raise ProviderAuthenticationError(f"An active access token is required to {action}.")
        return access_token

    # --- Meeting resolution ------------------------------------------------

    def resolve_meeting_by_join_url(
        self,
        join_web_url: str,
        access_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Resolve a Teams join URL to its onlineMeeting resource.

        Graph v1.0 exposes no way to list a user's online meetings, so filtering on
        ``JoinWebUrl`` is the supported lookup. This is also the manual fallback for
        meetings that never appear on the calendar.
        """
        token = self._require_session(access_token, "resolve a Teams meeting")
        url = str(join_web_url or "").strip()
        if not url:
            raise ProviderError("A Teams meeting join URL is required.")
        if not url.lower().startswith("http"):
            raise ProviderError(
                "That does not look like a Teams join URL. Copy the full "
                "'Join Microsoft Teams Meeting' link from the invitation."
            )

        # Graph requires the join URL to be URL-encoded inside the filter literal.
        quoted = urllib.parse.quote(url, safe="")
        body = self._json_body(
            self._graph_get(
                f"{self.GRAPH_BASE}/me/onlineMeetings?$filter=JoinWebUrl%20eq%20'{quoted}'",
                token,
            )
        )
        meetings = body.get("value") or []
        if not isinstance(meetings, list) or not meetings:
            raise TranscriptUnavailableError(
                "Microsoft Graph found no online meeting for that join URL. The meeting may have "
                "expired, may belong to another organizer, or may not be visible to this account."
            )
        first = meetings[0]
        return first if isinstance(first, dict) else {}

    def list_meeting_transcripts(
        self,
        meeting_id: str,
        access_token: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List the callTranscript resources attached to one online meeting."""
        token = self._require_session(access_token, "list Teams transcripts")
        identifier = str(meeting_id or "").strip()
        if not identifier:
            raise ProviderError("An online meeting id is required.")

        items, _ = self._paginate(
            f"{self.GRAPH_BASE}/me/onlineMeetings/{identifier}/transcripts", token
        )
        return [item for item in items if isinstance(item, dict)]

    # --- Discovery ---------------------------------------------------------

    def discover_transcripts(
        self,
        access_token: Optional[str] = None,
        event_probe_limit: Optional[int] = None,
        since_iso: Optional[str] = None,
        **kwargs,
    ) -> dict[str, Any]:
        """
        Discover Teams transcripts through the signed-in user's calendar.

        Graph v1.0 has no "list my online meetings" endpoint for delegated callers,
        so the only supported route is: calendar events that are online meetings ->
        each event's ``onlineMeeting.joinUrl`` -> ``$filter=JoinWebUrl eq '...'`` ->
        that meeting's transcripts.

        Returns ``{"transcripts": [...], "truncated": bool, "notes": [...]}``.
        Titles and dates come from the real calendar event; nothing is invented.
        """
        token = self._require_session(access_token, "list Teams transcripts")
        probe_limit = (
            self.DEFAULT_EVENT_PROBE_LIMIT if event_probe_limit is None else event_probe_limit
        )

        params = {
            "$select": "id,subject,start,end,isOnlineMeeting,onlineMeeting,organizer,attendees",
            "$top": self.EVENTS_PAGE_SIZE,
        }
        # Deliberately no $orderby: Graph rejects some filter+sort combinations on
        # /me/events with an "inefficient filter" error, so ordering is done below.
        filters = ["isOnlineMeeting eq true"]
        if since_iso:
            filters.append(f"start/dateTime ge '{since_iso}'")
        params["$filter"] = " and ".join(filters)

        # A 401/403 here is a real blocker for discovery (missing Calendars.Read or
        # un-consented permissions) and is reported rather than swallowed.
        events, events_truncated = self._paginate(f"{self.GRAPH_BASE}/me/events", token, params=params)

        notes: list[str] = []
        truncated = bool(events_truncated)
        if events_truncated:
            notes.append(
                "More online-meeting calendar events exist than were retrieved; "
                "older meetings were not examined."
            )

        events = [e for e in events if isinstance(e, dict)]
        events.sort(key=lambda e: str(((e.get("start") or {}).get("dateTime")) or ""), reverse=True)

        if not events:
            notes.append(
                "No calendar events flagged as online meetings were found for this account."
            )

        if len(events) > probe_limit:
            truncated = True
            notes.append(
                f"Checked the {probe_limit} most recent of {len(events)} online-meeting events "
                "for transcripts. Older events were not probed."
            )

        transcripts: list[dict[str, Any]] = []
        blocking_error = None

        for event in events[:probe_limit]:
            subject = str(event.get("subject") or "").strip() or None
            start = (event.get("start") or {}).get("dateTime") or None
            join_url = str((event.get("onlineMeeting") or {}).get("joinUrl") or "").strip()

            if not join_url:
                notes.append(
                    "Skipped '{}': the calendar event has no Teams join URL, so Graph cannot "
                    "resolve an online meeting for it.".format(subject or event.get("id") or "event")
                )
                continue

            try:
                meeting = self.resolve_meeting_by_join_url(join_url, access_token=token)
                meeting_id = str(meeting.get("id") or "").strip()
                if not meeting_id:
                    continue
                meeting_transcripts = self.list_meeting_transcripts(meeting_id, access_token=token)
            except ProviderTokenExpiredError:
                raise
            except ProviderConsentRequiredError as e:
                blocking_error = blocking_error or e
                notes.append(
                    "Could not read '{}': {}".format(subject or event.get("id") or "event", e)
                )
                continue
            except TranscriptUnavailableError as e:
                notes.append("No transcript for '{}': {}".format(subject or "event", e))
                continue
            except ProviderError as e:
                notes.append("Could not read '{}': {}".format(subject or "event", e))
                continue

            participants = self._event_participant_names(event)

            for item in meeting_transcripts:
                transcript_id = str(item.get("id") or "").strip()
                if not transcript_id:
                    continue
                created = item.get("createdDateTime") or start
                transcripts.append(
                    {
                        "id": f"{meeting_id}/transcripts/{transcript_id}",
                        "title": subject,
                        "date": created,
                        "display_label": "Teams · {} · {}".format(
                            subject or "Untitled meeting",
                            str(created)[:16].replace("T", " ") if created else "unknown time",
                        ),
                        "source_type": "ms_graph_transcript",
                        "participants": participants,
                        "available": True,
                        "details": {
                            "online_meeting_id": meeting_id,
                            "transcript_id": transcript_id,
                            "created_date_time": item.get("createdDateTime"),
                            "end_date_time": item.get("endDateTime"),
                            "event_start": start,
                            "event_end": (event.get("end") or {}).get("dateTime"),
                            "organizer": self._organizer_name(event),
                        },
                    }
                )

        if not transcripts and blocking_error is not None:
            # Nothing was retrievable and the reason was a permission problem: the
            # user needs to see that, not an empty list.
            raise blocking_error

        transcripts.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
        return {"transcripts": transcripts, "truncated": truncated, "notes": notes}

    def list_available_transcripts(self, access_token: Optional[str] = None) -> list[dict[str, Any]]:
        """Lists online meetings with available transcripts via Microsoft Graph."""
        return self.discover_transcripts(access_token=access_token)["transcripts"]

    @staticmethod
    def _organizer_name(event: dict) -> Optional[str]:
        organizer = ((event.get("organizer") or {}).get("emailAddress") or {})
        return str(organizer.get("name") or organizer.get("address") or "").strip() or None

    @staticmethod
    def _event_participant_names(event: dict) -> list[str]:
        """Real invitee names from the calendar event. No roles are inferred."""
        names: list[str] = []
        organizer = MSTeamsProvider._organizer_name(event)
        if organizer:
            names.append(organizer)
        for attendee in event.get("attendees") or []:
            if not isinstance(attendee, dict):
                continue
            email = attendee.get("emailAddress") or {}
            candidate = str(email.get("name") or email.get("address") or "").strip()
            if candidate and candidate not in names:
                names.append(candidate)
        return names

    # --- Transcript content ------------------------------------------------

    @staticmethod
    def _strip_tags(text: str) -> str:
        return re.sub(r"<[^>]*>", "", text)

    def parse_vtt(self, vtt_text: str) -> tuple:
        """
        Parse WebVTT into ``(text, speakers)``.

        Cues are handled structurally -- header, ``NOTE``/``STYLE``/``REGION``
        blocks, cue identifiers and timing lines are dropped by position rather
        than by guessing at content, so a line of dialogue that happens to be
        numeric or to start with "NOTE" is not lost.

        Speaker names come only from real VTT voice tags (``<v Name>``) or an
        explicit ``Name:`` prefix that Teams itself wrote. Nothing is invented and
        no role is ever attached to a name.
        """
        if not isinstance(vtt_text, str):
            raise TranscriptParseError("Transcript content was not text.")

        normalized = vtt_text.replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.split(r"\n\s*\n", normalized)

        lines: list = []
        speakers: list = []
        last_speaker = None

        for raw_block in blocks:
            block_lines = [line.strip() for line in raw_block.strip().splitlines()]
            block_lines = [line for line in block_lines if line]
            if not block_lines:
                continue

            first = block_lines[0]
            if first.startswith("WEBVTT"):
                continue
            if first.startswith(("NOTE", "STYLE", "REGION")):
                continue

            timing_index = None
            for index, line in enumerate(block_lines):
                if "-->" in line:
                    timing_index = index
                    break
            if timing_index is None:
                # Not a cue (stray metadata); nothing spoken to keep.
                continue

            payload_lines = block_lines[timing_index + 1:]
            if not payload_lines:
                continue
            payload = " ".join(payload_lines).strip()
            if not payload:
                continue

            speaker = None
            voice = re.match(r"^<v[^>\s]*\s+([^>]+)>", payload)
            if voice:
                speaker = voice.group(1).strip() or None
                payload = payload[voice.end():]

            text = self._strip_tags(payload).strip()
            if not text:
                continue

            if speaker is None:
                # Teams' unattributed export sometimes still carries "Name: text".
                inline = re.match(r"^([^:<>]{1,60}):\s+(\S.*)$", text)
                if inline:
                    candidate = inline.group(1).strip()
                    if candidate and len(candidate.split()) <= 6 and not candidate.endswith("."):
                        speaker = candidate
                        text = inline.group(2).strip()

            if speaker:
                if speaker not in speakers:
                    speakers.append(speaker)
                # Merge consecutive cues from one speaker without changing wording.
                if speaker == last_speaker and lines:
                    lines[-1] = f"{lines[-1]} {text}"
                else:
                    lines.append(f"{speaker}: {text}")
                    last_speaker = speaker
            else:
                lines.append(text)
                last_speaker = None

        return "\n".join(lines).strip(), speakers

    def _parse_vtt_content(self, vtt_text: str) -> str:
        """Parses WebVTT format into clean readable transcript text."""
        text, _ = self.parse_vtt(vtt_text)
        return text

    def _fetch_transcript_content(self, meeting_id: str, transcript_id: str, token: str):
        """
        Fetch transcript content, preferring speaker-attributed VTT.

        Teams can forbid speaker attribution for a tenant. That surfaces as HTTP 403
        ``SpeakerAttributionNotAllowed`` *only* when VTT is requested, and the
        documented remedy is to ask for the unattributed plain-text type via the
        Accept header (``$format`` does not support that type).
        """
        url = f"{self.GRAPH_BASE}/me/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content"

        try:
            response = self._graph_get(
                url,
                token,
                params={"$format": self.VTT_CONTENT_TYPE},
                extra_headers={"Accept": self.VTT_CONTENT_TYPE},
            )
            return response, True
        except ProviderConsentRequiredError as e:
            if "SpeakerAttributionNotAllowed" not in str(getattr(e, "graph_code", "")):
                raise
        except ProviderAPIError as e:
            if e.status_code != 406:
                raise

        # Retry without speaker attribution.
        response = self._graph_get(
            url, token, extra_headers={"Accept": self.PLAIN_CONTENT_TYPE}
        )
        return response, False

    def get_transcript(
        self,
        meeting_id: str,
        access_token: Optional[str] = None,
        meeting_title: Optional[str] = None,
    ) -> NormalizedTranscript:
        """
        Retrieves and normalizes a transcript from Microsoft Graph.

        ``meeting_id`` accepts ``{onlineMeetingId}/transcripts/{transcriptId}``
        (what discovery returns), a bare ``{onlineMeetingId}``, or a Teams join URL.
        ``meeting_title`` lets the caller pass the real calendar subject through,
        since the transcript resource itself has no title.
        """
        token = self._require_session(access_token, "retrieve transcripts")

        identifier = str(meeting_id or "").strip()
        if not identifier:
            raise ProviderError("No Teams meeting or transcript identifier was provided.")

        resolved_title = meeting_title
        transcript_id = None

        if identifier.lower().startswith("http"):
            meeting = self.resolve_meeting_by_join_url(identifier, access_token=token)
            online_meeting_id = str(meeting.get("id") or "").strip()
            resolved_title = resolved_title or (str(meeting.get("subject") or "").strip() or None)
        elif "/transcripts/" in identifier:
            online_meeting_id, _, transcript_id = identifier.partition("/transcripts/")
            online_meeting_id = online_meeting_id.strip().strip("/")
            transcript_id = transcript_id.strip().strip("/")
        else:
            online_meeting_id = identifier

        if not online_meeting_id:
            raise ProviderError("Could not determine which Teams meeting to read.")

        if not transcript_id:
            available = self.list_meeting_transcripts(online_meeting_id, access_token=token)
            if not available:
                raise TranscriptUnavailableError(
                    "Microsoft Graph reports no transcript for this Teams meeting. The meeting was "
                    "most likely never transcribed, or the meeting has expired."
                )
            # Newest transcript first.
            available.sort(key=lambda item: str(item.get("createdDateTime") or ""), reverse=True)
            transcript_id = str(available[0].get("id") or "").strip()
            if not transcript_id:
                raise TranscriptUnavailableError(
                    "Microsoft Graph returned a transcript without an id for this meeting."
                )

        # Best-effort metadata; a failure here must not block retrieval.
        created_date_time = None
        try:
            info = self._json_body(
                self._graph_get(
                    f"{self.GRAPH_BASE}/me/onlineMeetings/{online_meeting_id}"
                    f"/transcripts/{transcript_id}",
                    token,
                )
            )
            created_date_time = info.get("createdDateTime") or None
        except ProviderTokenExpiredError:
            raise
        except Exception:
            created_date_time = None

        response, speaker_attributed = self._fetch_transcript_content(
            online_meeting_id, transcript_id, token
        )

        body = str(getattr(response, "text", "") or "")
        content_type = str((getattr(response, "headers", {}) or {}).get("Content-Type", ""))

        speakers: list = []
        if "vtt" in content_type.lower() or body.lstrip().startswith("WEBVTT"):
            raw_text, speakers = self.parse_vtt(body)
            content_format = "text/vtt"
        else:
            # Unattributed plain text: preserved verbatim so evidence validation
            # can still match the original wording exactly.
            raw_text = body.strip()
            content_format = "text/plain"

        if not raw_text:
            raise TranscriptUnavailableError(
                "Microsoft Graph returned an empty transcript for this Teams meeting."
            )

        metadata: dict[str, Any] = {
            "meeting_resource": f"{online_meeting_id}/transcripts/{transcript_id}",
            "online_meeting_id": online_meeting_id,
            "transcript_id": transcript_id,
            "content_format": content_format,
            "speaker_attributed": speaker_attributed,
            "char_count": len(raw_text),
            "line_count": len(raw_text.splitlines()),
            "retrieved_via": "ms_graph_transcript_content",
        }
        if created_date_time:
            metadata["created_date_time"] = created_date_time
        if not speaker_attributed:
            metadata["speaker_attribution_note"] = (
                "This tenant does not permit speaker attribution, so the transcript has no "
                "speaker names."
            )

        return NormalizedTranscript(
            raw_text=raw_text,
            source="microsoft_teams",
            provider=self.name,
            transcript_id=f"{online_meeting_id}/transcripts/{transcript_id}",
            # Only a real calendar subject is used; the transcript resource has none.
            meeting_title=resolved_title,
            meeting_date=created_date_time,
            participants=speakers,
            metadata=metadata,
        )
