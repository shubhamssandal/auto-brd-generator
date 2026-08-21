"""
Google Meet provider tests.

Every Google API call is monkeypatched: no real credentials, no network access,
and no fabricated "success" — each test asserts on what the mocked API actually
returned.
"""

import pytest
import requests

from providers.base import (
    ProviderAPIError,
    ProviderConsentRequiredError,
    ProviderTokenExpiredError,
    TranscriptUnavailableError,
)
from providers.google_meet import GoogleMeetProvider
from providers.session_tokens import TokenSet, call_with_refresh


MEET = "https://meet.googleapis.com/v2"
DRIVE = "https://www.googleapis.com/drive/v3"


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "mock-client-id")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "mock-client-secret")
    return GoogleMeetProvider()


class FakeResponse:
    def __init__(self, body=None, status_code=200, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text
        self.headers = {}

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class Router:
    """Dispatches mocked requests.get calls by URL, recording every call."""

    def __init__(self, routes):
        # routes: list of (url_fragment, handler(params) -> FakeResponse)
        self.routes = routes
        self.calls = []

    def __call__(self, url, *args, **kwargs):
        params = kwargs.get("params") or {}
        self.calls.append((url, dict(params)))
        for fragment, handler in self.routes:
            if fragment in url:
                return handler(params) if callable(handler) else handler
        raise AssertionError(f"unexpected request to {url}")

    def install(self, monkeypatch):
        monkeypatch.setattr(requests, "get", self)
        return self

    def urls(self):
        return [url for url, _ in self.calls]


# --- Authorization URL ---

def test_authorization_url_carries_state_pkce_and_offline_access(provider):
    url = provider.get_authorization_url(
        "http://localhost:8501",
        state="signed.state.value",
        code_challenge="challenge-abc",
        code_challenge_method="S256",
    )

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "state=signed.state.value" in url
    assert "code_challenge=challenge-abc" in url
    assert "code_challenge_method=S256" in url
    # access_type=offline + prompt=consent is what makes Google return a refresh token.
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    # Never send the client secret to the authorization endpoint.
    assert "mock-client-secret" not in url


def test_authorization_url_omits_pkce_when_not_supplied(provider):
    url = provider.get_authorization_url("http://localhost:8501", state="s")
    assert "code_challenge" not in url


# --- Token exchange / refresh error translation ---

def test_token_exchange_sends_the_pkce_verifier(provider, monkeypatch):
    captured = {}

    class MockPost:
        status_code = 200
        def json(self):
            return {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3599}

    def fake_post(url, data=None, **kwargs):
        captured.update(data or {})
        return MockPost()

    monkeypatch.setattr(requests, "post", fake_post)

    tokens = provider.exchange_code_for_token(
        "auth-code", "http://localhost:8501", code_verifier="verifier-xyz"
    )

    assert tokens["access_token"] == "at-1"
    assert captured["code_verifier"] == "verifier-xyz"
    assert captured["grant_type"] == "authorization_code"


def test_invalid_authorization_code_is_explained(provider, monkeypatch):
    class MockPost:
        status_code = 400
        def json(self):
            return {"error": "invalid_grant", "error_description": "Bad Request"}

    monkeypatch.setattr(requests, "post", lambda *a, **kw: MockPost())

    with pytest.raises(Exception) as excinfo:
        provider.exchange_code_for_token("stale-code", "http://localhost:8501")

    message = str(excinfo.value)
    assert "invalid" in message.lower()
    assert "mock-client-secret" not in message


def test_token_endpoint_failure_never_echoes_the_client_secret(provider, monkeypatch):
    class MockPost:
        status_code = 500
        def json(self):
            return {}

    monkeypatch.setattr(requests, "post", lambda *a, **kw: MockPost())

    with pytest.raises(Exception) as excinfo:
        provider.refresh_access_token("rt-1")
    assert "mock-client-secret" not in str(excinfo.value)


# --- HTTP error translation ---

@pytest.mark.parametrize(
    "status,expected",
    [
        (401, ProviderTokenExpiredError),
        (403, ProviderConsentRequiredError),
        (404, ProviderAPIError),
        (429, ProviderAPIError),
        (500, ProviderAPIError),
    ],
)
def test_http_errors_map_to_typed_errors(provider, monkeypatch, status, expected):
    body = {"error": {"code": status, "message": "mock failure detail"}}
    monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(body, status_code=status))

    with pytest.raises(expected):
        provider.get_transcript("conferenceRecords/rec-1/transcripts/t-1", access_token="at-1")


def test_network_error_is_reported_as_api_error(provider, monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("connection reset")

    monkeypatch.setattr(requests, "get", boom)

    with pytest.raises(ProviderAPIError, match="Network error"):
        provider.get_transcript("drive-doc-1", access_token="at-1")


# --- Pagination ---

def test_pagination_follows_next_page_token(provider, monkeypatch):
    pages = {
        None: {"conferenceRecords": [{"name": "conferenceRecords/1"}], "nextPageToken": "p2"},
        "p2": {"conferenceRecords": [{"name": "conferenceRecords/2"}], "nextPageToken": "p3"},
        "p3": {"conferenceRecords": [{"name": "conferenceRecords/3"}]},
    }

    router = Router([
        ("conferenceRecords", lambda params: FakeResponse(pages[params.get("pageToken")])),
    ]).install(monkeypatch)

    items, truncated = provider._paginate(
        f"{MEET}/conferenceRecords", "at-1", "conferenceRecords", max_pages=10
    )

    assert [i["name"] for i in items] == [
        "conferenceRecords/1", "conferenceRecords/2", "conferenceRecords/3"
    ]
    assert truncated is False
    # Every page must be requested at Google's maximum page size.
    assert all(params["pageSize"] == 100 for _, params in router.calls)
    assert len(router.calls) == 3


def test_pagination_reports_truncation_instead_of_silently_stopping(provider, monkeypatch):
    Router([
        ("conferenceRecords", lambda params: FakeResponse(
            {"conferenceRecords": [{"name": "conferenceRecords/x"}], "nextPageToken": "always-more"}
        )),
    ]).install(monkeypatch)

    items, truncated = provider._paginate(
        f"{MEET}/conferenceRecords", "at-1", "conferenceRecords", max_pages=3
    )

    assert len(items) == 3
    assert truncated is True


# --- Discovery ---

def test_discovery_lists_meet_transcripts_and_drive_documents(provider, monkeypatch):
    def route_records(params):
        return FakeResponse({
            "conferenceRecords": [
                {
                    "name": "conferenceRecords/rec-1",
                    "space": "spaces/space-1",
                    "startTime": "2026-08-20T09:00:00Z",
                }
            ]
        })

    def route_transcripts(params):
        return FakeResponse({
            "transcripts": [
                {
                    "name": "conferenceRecords/rec-1/transcripts/t-1",
                    "state": "FILE_GENERATED",
                    "startTime": "2026-08-20T09:01:00Z",
                    "docsDestination": {"document": "doc-linked-to-meet"},
                }
            ]
        })

    def route_space(params):
        return FakeResponse({"name": "spaces/space-1", "meetingCode": "abc-defg-hij"})

    def route_drive_files(params):
        return FakeResponse({
            "files": [
                # Already represented by the Meet transcript above: must be deduplicated.
                {"id": "doc-linked-to-meet", "name": "Transcript - Duplicate"},
                {
                    "id": "doc-standalone",
                    "name": "Roadmap Review - Transcript",
                    "createdTime": "2026-07-01T12:00:00Z",
                },
            ]
        })

    Router([
        ("/transcripts", route_transcripts),
        ("/conferenceRecords", route_records),
        ("/spaces/", route_space),
        (f"{DRIVE}/files", route_drive_files),
    ]).install(monkeypatch)

    result = provider.discover_transcripts(access_token="at-1")
    entries = result["transcripts"]

    assert result["truncated"] is False
    assert len(entries) == 2

    meet_entry = next(e for e in entries if e["source_type"] == "meet_api")
    assert meet_entry["id"] == "conferenceRecords/rec-1/transcripts/t-1"
    # The Meet API exposes no meeting subject, so no title may be invented.
    assert meet_entry["title"] is None
    assert meet_entry["date"] == "2026-08-20T09:01:00Z"
    assert meet_entry["available"] is True
    assert meet_entry["details"]["meeting_code"] == "abc-defg-hij"
    assert "abc-defg-hij" in meet_entry["display_label"]

    drive_entry = next(e for e in entries if e["source_type"] == "drive_doc")
    assert drive_entry["id"] == "doc-standalone"
    assert drive_entry["title"] == "Roadmap Review - Transcript"
    assert "doc-linked-to-meet" not in [e["id"] for e in entries]


def test_discovery_marks_transcripts_that_are_not_ready(provider, monkeypatch):
    Router([
        ("/transcripts", lambda params: FakeResponse({
            "transcripts": [{"name": "conferenceRecords/rec-1/transcripts/t-1", "state": "STARTED"}]
        })),
        ("/conferenceRecords", lambda params: FakeResponse({
            "conferenceRecords": [{"name": "conferenceRecords/rec-1", "space": "spaces/s1"}]
        })),
        ("/spaces/", lambda params: FakeResponse({})),
        (f"{DRIVE}/files", lambda params: FakeResponse({"files": []})),
    ]).install(monkeypatch)

    entries = provider.discover_transcripts(access_token="at-1")["transcripts"]
    assert len(entries) == 1
    assert entries[0]["available"] is False
    assert "not ready" in entries[0]["display_label"]


def test_discovery_reports_no_meetings_without_inventing_any(provider, monkeypatch):
    Router([
        ("/conferenceRecords", lambda params: FakeResponse({})),
        (f"{DRIVE}/files", lambda params: FakeResponse({"files": []})),
    ]).install(monkeypatch)

    result = provider.discover_transcripts(access_token="at-1")

    assert result["transcripts"] == []
    assert any("no conference records" in note.lower() for note in result["notes"])


def test_discovery_survives_a_drive_failure_and_says_so(provider, monkeypatch):
    Router([
        ("/transcripts", lambda params: FakeResponse({
            "transcripts": [{"name": "conferenceRecords/rec-1/transcripts/t-1", "state": "FILE_GENERATED"}]
        })),
        ("/conferenceRecords", lambda params: FakeResponse({
            "conferenceRecords": [{"name": "conferenceRecords/rec-1", "space": "spaces/s1"}]
        })),
        ("/spaces/", lambda params: FakeResponse({})),
        (f"{DRIVE}/files", lambda params: FakeResponse(
            {"error": {"message": "Drive API has not been used"}}, status_code=403
        )),
    ]).install(monkeypatch)

    result = provider.discover_transcripts(access_token="at-1")

    assert len(result["transcripts"]) == 1
    assert any("Drive" in note for note in result["notes"])


def test_discovery_reports_probe_limit_truncation(provider, monkeypatch):
    records = [
        {"name": f"conferenceRecords/rec-{i}", "space": "spaces/s1", "startTime": "2026-08-01T00:00:00Z"}
        for i in range(5)
    ]

    Router([
        ("/transcripts", lambda params: FakeResponse({"transcripts": []})),
        ("/conferenceRecords", lambda params: FakeResponse({"conferenceRecords": records})),
        ("/spaces/", lambda params: FakeResponse({})),
        (f"{DRIVE}/files", lambda params: FakeResponse({"files": []})),
    ]).install(monkeypatch)

    result = provider.discover_transcripts(access_token="at-1", record_probe_limit=2)

    assert result["truncated"] is True
    assert any("2 most recent of 5" in note for note in result["notes"])


def test_discovery_propagates_expired_token(provider, monkeypatch):
    Router([
        ("/conferenceRecords", lambda params: FakeResponse(
            {"error": {"message": "Invalid Credentials"}}, status_code=401
        )),
    ]).install(monkeypatch)

    with pytest.raises(ProviderTokenExpiredError):
        provider.discover_transcripts(access_token="expired-token")


# --- Transcript retrieval from Meet API entries ---

def _entry(participant, text, start="2026-08-20T09:05:00Z"):
    return {
        "name": "conferenceRecords/rec-1/transcripts/t-1/entries/e",
        "participant": participant,
        "text": text,
        "languageCode": "en-US",
        "startTime": start,
    }


def test_get_transcript_resolves_real_participant_names(provider, monkeypatch):
    entry_pages = {
        None: {
            "transcriptEntries": [
                _entry("conferenceRecords/rec-1/participants/p1", "Support volume is up 30%."),
                _entry("conferenceRecords/rec-1/participants/p1", "Mostly refund questions."),
                _entry("conferenceRecords/rec-1/participants/p2", "We need email notifications."),
            ],
            "nextPageToken": "e2",
        },
        "e2": {
            "transcriptEntries": [
                _entry("conferenceRecords/rec-1/participants/p2", "Ship it this sprint."),
            ]
        },
    }

    participants = {
        "p1": {"signedinUser": {"displayName": "Sarah"}},
        "p2": {"signedinUser": {"displayName": "Priya"}},
    }

    router = Router([])

    def dispatch(url, *args, **kwargs):
        params = kwargs.get("params") or {}
        router.calls.append((url, dict(params)))
        if "/entries" in url:
            return FakeResponse(entry_pages[params.get("pageToken")])
        if "/participants/" in url:
            return FakeResponse(participants[url.rsplit("/", 1)[-1]])
        if "/transcripts/t-1" in url:
            return FakeResponse({
                "name": "conferenceRecords/rec-1/transcripts/t-1",
                "state": "FILE_GENERATED",
                "startTime": "2026-08-20T09:00:00Z",
            })
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr(requests, "get", dispatch)

    transcript = provider.get_transcript(
        "conferenceRecords/rec-1/transcripts/t-1", access_token="at-1"
    )

    assert transcript.source == "google_meet"
    assert transcript.provider == "google_meet"
    assert transcript.transcript_id == "conferenceRecords/rec-1/transcripts/t-1"
    assert transcript.meeting_date == "2026-08-20T09:00:00Z"
    # Real names from the participants resource, with no invented roles attached.
    assert transcript.participants == ["Sarah", "Priya"]
    assert "Sarah — " not in transcript.raw_text
    # Consecutive entries from one speaker are joined; wording is preserved verbatim.
    assert "Sarah: Support volume is up 30%. Mostly refund questions." in transcript.raw_text
    assert "Priya: We need email notifications. Ship it this sprint." in transcript.raw_text
    assert transcript.metadata["entry_count"] == 4
    assert transcript.metadata["unresolved_participant_count"] == 0
    assert transcript.metadata["retrieved_via"] == "meet_api_entries"
    assert transcript.metadata["language_codes"] == ["en-US"]


def test_unnamed_participants_get_neutral_placeholders(provider, monkeypatch):
    """An unnamed speaker must never be given an invented identity."""
    def dispatch(url, *args, **kwargs):
        params = kwargs.get("params") or {}
        if "/entries" in url:
            return FakeResponse({
                "transcriptEntries": [
                    _entry("conferenceRecords/rec-1/participants/p1", "We should log refunds."),
                    _entry("conferenceRecords/rec-1/participants/p2", "Agreed."),
                ]
            })
        if "/participants/" in url:
            # Google returns the resource but with no display name anywhere.
            return FakeResponse({"name": url.rsplit("/v2/", 1)[-1]})
        return FakeResponse({"state": "FILE_GENERATED"})

    monkeypatch.setattr(requests, "get", dispatch)

    transcript = provider.get_transcript(
        "conferenceRecords/rec-1/transcripts/t-1", access_token="at-1"
    )

    assert transcript.participants == []
    assert "Speaker 1: We should log refunds." in transcript.raw_text
    assert "Speaker 2: Agreed." in transcript.raw_text
    assert transcript.metadata["unresolved_participant_count"] == 2


def test_entry_truncation_is_flagged_not_hidden(provider, monkeypatch):
    def dispatch(url, *args, **kwargs):
        if "/entries" in url:
            return FakeResponse({
                "transcriptEntries": [
                    _entry("conferenceRecords/rec-1/participants/p1", "More to come.")
                ],
                "nextPageToken": "never-ends",
            })
        if "/participants/" in url:
            return FakeResponse({"signedinUser": {"displayName": "Sarah"}})
        return FakeResponse({"state": "FILE_GENERATED"})

    monkeypatch.setattr(requests, "get", dispatch)
    monkeypatch.setattr(provider, "MAX_ENTRY_PAGES", 2, raising=False)

    transcript = provider.get_transcript(
        "conferenceRecords/rec-1/transcripts/t-1", access_token="at-1"
    )

    assert transcript.metadata["entries_truncated"] is True
    assert "incomplete" in transcript.metadata["truncation_warning"]


def test_missing_entries_fall_back_to_the_drive_document(provider, monkeypatch):
    """The Google Docs artifact is the only remaining real source when entries are absent."""
    doc_text = "Attendees\nSarah, Priya\n\nSarah: Refund SLA must be 24 hours."

    def dispatch(url, *args, **kwargs):
        params = kwargs.get("params") or {}
        if "/entries" in url:
            return FakeResponse({"transcriptEntries": []})
        if "/export" in url:
            assert params.get("mimeType") == "text/plain"
            return FakeResponse(status_code=200, text=doc_text)
        if f"{DRIVE}/files/" in url:
            return FakeResponse({"id": "doc-abc", "name": "Weekly Sync - Transcript",
                                 "createdTime": "2026-08-20T09:00:00Z"})
        return FakeResponse({
            "state": "FILE_GENERATED",
            "docsDestination": {"document": "doc-abc"},
        })

    monkeypatch.setattr(requests, "get", dispatch)

    transcript = provider.get_transcript(
        "conferenceRecords/rec-1/transcripts/t-1", access_token="at-1"
    )

    assert transcript.metadata["retrieved_via"] == "drive_export_fallback"
    assert transcript.metadata["file_id"] == "doc-abc"
    assert transcript.metadata["meet_transcript_name"] == "conferenceRecords/rec-1/transcripts/t-1"
    assert transcript.meeting_title == "Weekly Sync - Transcript"
    # Verbatim text is what evidence validation matches against.
    assert "Sarah: Refund SLA must be 24 hours." in transcript.raw_text
    assert transcript.participants == ["Sarah", "Priya"]


def test_transcript_still_recording_is_reported_clearly(provider, monkeypatch):
    def dispatch(url, *args, **kwargs):
        if "/entries" in url:
            return FakeResponse({"transcriptEntries": []})
        return FakeResponse({"state": "STARTED"})

    monkeypatch.setattr(requests, "get", dispatch)

    with pytest.raises(TranscriptUnavailableError, match="still being recorded"):
        provider.get_transcript("conferenceRecords/rec-1/transcripts/t-1", access_token="at-1")


def test_transcript_file_not_generated_yet_is_reported_clearly(provider, monkeypatch):
    def dispatch(url, *args, **kwargs):
        if "/entries" in url:
            return FakeResponse({"transcriptEntries": []})
        return FakeResponse({"state": "ENDED"})

    monkeypatch.setattr(requests, "get", dispatch)

    with pytest.raises(TranscriptUnavailableError, match="not finished generating"):
        provider.get_transcript("conferenceRecords/rec-1/transcripts/t-1", access_token="at-1")


def test_conference_record_without_transcript_explains_transcription_requirement(provider, monkeypatch):
    Router([
        ("/transcripts", lambda params: FakeResponse({"transcripts": []})),
    ]).install(monkeypatch)

    with pytest.raises(TranscriptUnavailableError, match="never transcribed"):
        provider.get_transcript("conferenceRecords/rec-1", access_token="at-1")


def test_conference_record_prefers_the_generated_transcript(provider, monkeypatch):
    def dispatch(url, *args, **kwargs):
        if url.endswith("/transcripts"):
            return FakeResponse({"transcripts": [
                {"name": "conferenceRecords/rec-1/transcripts/t-pending", "state": "ENDED"},
                {"name": "conferenceRecords/rec-1/transcripts/t-ready", "state": "FILE_GENERATED"},
            ]})
        if "/entries" in url:
            assert "t-ready" in url
            return FakeResponse({"transcriptEntries": [
                _entry("conferenceRecords/rec-1/participants/p1", "Decision recorded.")
            ]})
        if "/participants/" in url:
            return FakeResponse({"signedinUser": {"displayName": "Rahul"}})
        return FakeResponse({"state": "FILE_GENERATED"})

    monkeypatch.setattr(requests, "get", dispatch)

    transcript = provider.get_transcript("conferenceRecords/rec-1", access_token="at-1")
    assert transcript.transcript_id == "conferenceRecords/rec-1/transcripts/t-ready"
    assert "Rahul: Decision recorded." in transcript.raw_text


def test_missing_drive_document_is_reported_as_unavailable(provider, monkeypatch):
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **kw: FakeResponse({"error": {"message": "File not found"}}, status_code=404),
    )

    with pytest.raises(TranscriptUnavailableError, match="no document with id"):
        provider.get_transcript("missing-doc", access_token="at-1")


def test_empty_drive_export_is_reported_as_unavailable(provider, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: FakeResponse(status_code=200, text="   "))

    with pytest.raises(TranscriptUnavailableError, match="empty transcript document"):
        provider.get_transcript("blank-doc", access_token="at-1")


# --- Refresh-and-retry against the real provider code path ---

def test_expired_token_triggers_refresh_then_succeeds(provider, monkeypatch):
    """A 401 from Meet must refresh once and retry, transparently to the caller."""
    state = {"calls": 0}

    def dispatch(url, *args, **kwargs):
        headers = kwargs.get("headers") or {}
        if headers.get("Authorization") == "Bearer stale-token":
            state["calls"] += 1
            return FakeResponse({"error": {"message": "Invalid Credentials"}}, status_code=401)
        if "/entries" in url:
            return FakeResponse({"transcriptEntries": [
                _entry("conferenceRecords/rec-1/participants/p1", "Renewed session works.")
            ]})
        if "/participants/" in url:
            return FakeResponse({"signedinUser": {"displayName": "Sarah"}})
        return FakeResponse({"state": "FILE_GENERATED"})

    monkeypatch.setattr(requests, "get", dispatch)

    class MockPost:
        status_code = 200
        def json(self):
            return {"access_token": "fresh-token", "expires_in": 3599}

    monkeypatch.setattr(requests, "post", lambda *a, **kw: MockPost())

    tokens = TokenSet.from_response({"access_token": "stale-token", "refresh_token": "rt-1"})
    transcript, updated = call_with_refresh(
        provider,
        tokens,
        lambda token: provider.get_transcript(
            "conferenceRecords/rec-1/transcripts/t-1", access_token=token
        ),
    )

    assert state["calls"] >= 1
    assert updated.access_token == "fresh-token"
    assert updated.refresh_token == "rt-1"
    assert "Sarah: Renewed session works." in transcript.raw_text


# --- Participant extraction from exported documents ---

def test_speaker_labels_are_extracted_without_inventing_roles():
    text = (
        "Priya (PM): We need email notifications.\n"
        "Rahul: Let's do a spike first.\n"
        "This line is narrative prose and not a speaker label.\n"
    )
    names = GoogleMeetProvider._extract_participants_from_text(text)

    # The role is kept only because the source actually stated it.
    assert "Priya (PM)" in names
    assert "Rahul" in names
    assert not any("Product Manager" in n for n in names)


def test_attendees_block_is_preferred_over_speaker_labels():
    text = "Attendees\nSarah, Priya, Rahul\n\nSarah: Support volume is up 30%."
    assert GoogleMeetProvider._extract_participants_from_text(text) == ["Sarah", "Priya", "Rahul"]
