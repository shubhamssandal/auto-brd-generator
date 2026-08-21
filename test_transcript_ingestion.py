import io
import os
import pytest
from brd_models import NormalizedTranscript
from transcript_processor import (
    normalize_manual_notes,
    normalize_uploaded_file,
    extract_text_from_file_bytes,
    TranscriptProcessingError,
)
from providers.google_meet import GoogleMeetProvider
from providers.ms_teams import MSTeamsProvider
from providers.base import ProviderNotConfiguredError
from main import validate_and_create_brd_data


# --- Manual Ingestion Tests ---

def test_normalize_manual_notes_valid():
    text = "Team decided to build a notification system. Push notifications deferred."
    transcript = normalize_manual_notes(text, title="Sprint Review")
    assert transcript.source == "manual"
    assert transcript.raw_text == text
    assert transcript.meeting_title == "Sprint Review"
    assert transcript.metadata["char_count"] == len(text)
    assert transcript.metadata["line_count"] == 1


def test_normalize_manual_notes_empty():
    with pytest.raises(TranscriptProcessingError, match="cannot be empty"):
        normalize_manual_notes("")


def test_normalize_manual_notes_whitespace_only():
    with pytest.raises(TranscriptProcessingError, match="cannot be empty"):
        normalize_manual_notes("   \n\t  \n  ")


# --- File Upload Ingestion Tests ---

def test_extract_text_from_utf8_bytes():
    raw = "Project: Alpha\nRequirement: Enable dark mode.".encode("utf-8")
    text = extract_text_from_file_bytes(raw, "notes.txt")
    assert "Project: Alpha" in text


def test_extract_text_from_latin1_bytes():
    # Character 0xE9 is é in latin-1, which would fail strict utf-8 decoding
    raw = b"Caf\xe9 feature discussion: must support french accents."
    text = extract_text_from_file_bytes(raw, "notes.txt")
    assert "Caf" in text
    assert "french accents" in text


def test_extract_text_from_empty_bytes():
    with pytest.raises(TranscriptProcessingError, match="is empty"):
        extract_text_from_file_bytes(b"", "empty.txt")


def test_extract_text_from_whitespace_bytes():
    with pytest.raises(TranscriptProcessingError, match="contains only whitespace"):
        extract_text_from_file_bytes(b"   \n\t  \n  ", "spaces.txt")


def test_normalize_uploaded_file_bytes():
    content = b"Priya: We need export to PDF feature.\nRahul: Agreed for v2."
    transcript = normalize_uploaded_file(content, filename="q3_planning_sync.txt")
    assert transcript.source == "upload"
    assert transcript.meeting_title == "Q3 Planning Sync"
    assert "Priya: We need export to PDF feature." in transcript.raw_text
    assert transcript.metadata["filename"] == "q3_planning_sync.txt"
    assert transcript.metadata["line_count"] == 2


def test_normalize_uploaded_file_io_stream():
    content = io.BytesIO(b"Discussion on security policies.")
    content.name = "security_audit.txt"
    transcript = normalize_uploaded_file(content)
    assert transcript.source == "upload"
    assert transcript.meeting_title == "Security Audit"
    assert transcript.raw_text == "Discussion on security policies."


def test_normalize_uploaded_file_invalid_extension():
    content = io.BytesIO(b"Binary audio content")
    content.name = "recording.mp3"
    with pytest.raises(TranscriptProcessingError, match="Unsupported file format"):
        normalize_uploaded_file(content)


def test_normalize_uploaded_none():
    with pytest.raises(TranscriptProcessingError, match="No file was provided"):
        normalize_uploaded_file(None)


# --- Provider Adaptor Tests ---

def test_google_meet_provider_unconfigured(monkeypatch):
    monkeypatch.delenv("GOOGLE_WORKSPACE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_WORKSPACE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)

    provider = GoogleMeetProvider()
    assert provider.name == "google_meet"
    assert provider.display_name == "Google Meet"
    assert provider.is_configured() is False
    missing = provider.get_missing_configuration()
    assert any("GOOGLE_WORKSPACE_CLIENT_ID" in m for m in missing)
    assert any("GOOGLE_WORKSPACE_CLIENT_SECRET" in m for m in missing)
    assert "Google Cloud Project" in provider.get_setup_instructions()

    with pytest.raises(ProviderNotConfiguredError):
        provider.get_authorization_url("http://localhost:8501")

    with pytest.raises(ProviderNotConfiguredError):
        provider.exchange_code_for_token("fake-code", "http://localhost:8501")

    with pytest.raises(ProviderNotConfiguredError):
        provider.list_available_transcripts(access_token="fake-token")

    with pytest.raises(ProviderNotConfiguredError):
        provider.get_transcript("test-meet-123", access_token="fake-token")


def test_google_meet_provider_configured(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "mock-google-client-id")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "mock-google-client-secret")

    provider = GoogleMeetProvider()
    assert provider.is_configured() is True
    assert provider.get_missing_configuration() == []

    auth_url = provider.get_authorization_url("http://localhost:8501", state="google_meet")
    assert "accounts.google.com" in auth_url
    assert "mock-google-client-id" in auth_url
    assert "state=google_meet" in auth_url


def test_google_meet_token_exchange(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "mock-id")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "mock-secret")

    provider = GoogleMeetProvider()

    class MockResponse:
        status_code = 200
        def json(self):
            return {"access_token": "mock-access-token", "refresh_token": "mock-refresh"}

    import requests
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: MockResponse())

    tokens = provider.exchange_code_for_token("auth-code", "http://localhost:8501")
    assert tokens["access_token"] == "mock-access-token"


def test_google_meet_get_transcript_drive_export(monkeypatch):
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "mock-id")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "mock-secret")

    provider = GoogleMeetProvider()

    class MockDriveExportResponse:
        status_code = 200
        text = "Priya: We need to export BRD to markdown.\nRahul: Approved."

    import requests
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: MockDriveExportResponse())

    transcript = provider.get_transcript("drive-file-id-123", access_token="mock-token")
    assert transcript.source == "google_meet"
    assert "Priya: We need to export BRD to markdown." in transcript.raw_text
    assert transcript.metadata["file_id"] == "drive-file-id-123"


def test_ms_teams_provider_unconfigured(monkeypatch):
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    monkeypatch.delenv("AZURE_TENANT_ID", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_SECRET", raising=False)

    provider = MSTeamsProvider()
    assert provider.name == "microsoft_teams"
    assert provider.display_name == "Microsoft Teams"
    assert provider.is_configured() is False
    missing = provider.get_missing_configuration()
    assert "AZURE_CLIENT_ID" in missing
    assert "AZURE_TENANT_ID" in missing
    assert "AZURE_CLIENT_SECRET" in missing
    assert "Microsoft Entra ID" in provider.get_setup_instructions()

    with pytest.raises(ProviderNotConfiguredError):
        provider.get_authorization_url("http://localhost:8501")

    with pytest.raises(ProviderNotConfiguredError):
        provider.exchange_code_for_token("fake-code", "http://localhost:8501")

    with pytest.raises(ProviderNotConfiguredError):
        provider.list_available_transcripts(access_token="fake-token")

    with pytest.raises(ProviderNotConfiguredError):
        provider.get_transcript("test-teams-meeting-id", access_token="fake-token")


def test_ms_teams_provider_configured(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "mock-azure-id")
    monkeypatch.setenv("AZURE_TENANT_ID", "mock-tenant-id")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "mock-azure-secret")

    provider = MSTeamsProvider()
    assert provider.is_configured() is True
    assert provider.get_missing_configuration() == []

    auth_url = provider.get_authorization_url("http://localhost:8501", state="microsoft_teams")
    assert "login.microsoftonline.com/mock-tenant-id" in auth_url
    assert "mock-azure-id" in auth_url
    assert "state=microsoft_teams" in auth_url


def test_ms_teams_token_exchange(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "mock-azure-id")
    monkeypatch.setenv("AZURE_TENANT_ID", "mock-tenant-id")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "mock-azure-secret")

    provider = MSTeamsProvider()

    class MockResponse:
        status_code = 200
        def json(self):
            return {"access_token": "mock-teams-token", "refresh_token": "mock-refresh-token"}

    import requests
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: MockResponse())

    tokens = provider.exchange_code_for_token("auth-code", "http://localhost:8501")
    assert tokens["access_token"] == "mock-teams-token"


def test_ms_teams_token_exchange_error(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "mock-azure-id")
    monkeypatch.setenv("AZURE_TENANT_ID", "mock-tenant-id")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "mock-azure-secret")

    provider = MSTeamsProvider()

    class MockErrorResponse:
        status_code = 400
        def json(self):
            return {"error": "invalid_grant", "error_description": "AADSTS70008: The provided authorization code has expired."}

    import requests
    from providers.base import ProviderAuthenticationError
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: MockErrorResponse())

    with pytest.raises(ProviderAuthenticationError, match="AADSTS70008"):
        provider.exchange_code_for_token("expired-code", "http://localhost:8501")


def test_ms_teams_refresh_token(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "mock-azure-id")
    monkeypatch.setenv("AZURE_TENANT_ID", "mock-tenant-id")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "mock-azure-secret")

    provider = MSTeamsProvider()

    class MockRefreshResponse:
        status_code = 200
        def json(self):
            return {"access_token": "new-access-token"}

    import requests
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: MockRefreshResponse())

    result = provider.refresh_access_token("refresh-token-123")
    assert result["access_token"] == "new-access-token"


def test_ms_teams_list_transcripts(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "mock-azure-id")
    monkeypatch.setenv("AZURE_TENANT_ID", "mock-tenant-id")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "mock-azure-secret")

    provider = MSTeamsProvider()

    class MockMeetingsResponse:
        status_code = 200
        def json(self):
            return {"value": [{"id": "meeting-1", "subject": "Architecture Sync"}]}

    class MockTranscriptsResponse:
        status_code = 200
        def json(self):
            return {"value": [{"id": "transcript-1", "createdDateTime": "2026-08-21T10:00:00Z"}]}

    def mock_get(url, *args, **kwargs):
        if "/transcripts" in url:
            return MockTranscriptsResponse()
        return MockMeetingsResponse()

    import requests
    monkeypatch.setattr(requests, "get", mock_get)

    transcripts = provider.list_available_transcripts(access_token="mock-token")
    assert len(transcripts) == 1
    assert transcripts[0]["title"] == "Architecture Sync"
    assert transcripts[0]["id"] == "meeting-1/transcripts/transcript-1"


def test_ms_teams_list_transcripts_admin_consent_error(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "mock-azure-id")
    monkeypatch.setenv("AZURE_TENANT_ID", "mock-tenant-id")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "mock-azure-secret")

    provider = MSTeamsProvider()

    class MockForbiddenResponse:
        status_code = 403
        text = "Forbidden"
        def json(self):
            return {"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges."}}

    import requests
    from providers.base import ProviderAuthenticationError
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: MockForbiddenResponse())

    with pytest.raises(ProviderAuthenticationError, match="admin consent"):
        provider.list_available_transcripts(access_token="mock-token")


def test_ms_teams_vtt_parsing():
    provider = MSTeamsProvider()
    vtt_sample = """WEBVTT

NOTE Meeting recording transcript

1
00:00:01.000 --> 00:00:04.000
<v Sarah>Support volume is <b>up 30%</b>.</v>

2
00:00:05.000 --> 00:00:08.000
<v Priya>We will implement <i>status notifications</i>.</v>
"""
    cleaned = provider._parse_vtt_content(vtt_sample)
    assert "Sarah: Support volume is up 30%." in cleaned
    assert "Priya: We will implement status notifications." in cleaned
    assert "WEBVTT" not in cleaned
    assert "NOTE" not in cleaned
    assert "<b>" not in cleaned
    assert "-->" not in cleaned


def test_ms_teams_get_transcript(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "mock-azure-id")
    monkeypatch.setenv("AZURE_TENANT_ID", "mock-tenant-id")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "mock-azure-secret")

    provider = MSTeamsProvider()

    class MockGraphResponse:
        status_code = 200
        headers = {"Content-Type": "text/vtt"}
        text = "WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\n<v Rahul>Let's do a spike first.</v>"

    import requests
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: MockGraphResponse())

    transcript = provider.get_transcript("meeting123/transcripts/trans456", access_token="mock-token")
    assert transcript.source == "microsoft_teams"
    assert "Rahul: Let's do a spike first." in transcript.raw_text


# --- Pipeline Integration with NormalizedTranscript ---

def test_pipeline_with_normalized_transcript():
    source_notes = "Meeting: Payment Integration\nRahul: Payments DB needs webhook retry."
    transcript = NormalizedTranscript(
        raw_text=source_notes,
        source="upload",
        meeting_title="Payment Integration",
    )

    mock_gemini_output = {
        "project_title": "Payment Integration BRD",
        "functional_requirements": [
            {
                "requirement_id": "FR-1",
                "statement": "The system shall implement webhook retry.",
                "source_evidence": "Payments DB needs webhook retry.",
            }
        ],
        "assumptions": [],
    }

    brd = validate_and_create_brd_data(mock_gemini_output, transcript.raw_text)
    assert len(brd.functional_requirements) == 1
    assert brd.functional_requirements[0].requirement_id == "FR-1"
    assert brd.project_title == "Payment Integration BRD"


def test_all_four_sources_unified_pipeline(monkeypatch):
    """
    Verifies that all 4 sources (Manual, TXT Upload, Google Meet, MS Teams)
    produce a NormalizedTranscript that runs through validation and creates valid BRDData and Markdown.
    """
    from main import convert_brd_to_markdown

    common_evidence = "Team agreed to build customer refund tracking dashboard."
    raw_text = f"Discussion Notes:\n{common_evidence}\nPush notifications parked for v2."

    # 1. Manual Paste
    manual_t = normalize_manual_notes(raw_text, title="Manual Sync")
    # 2. TXT Upload
    upload_t = normalize_uploaded_file(raw_text.encode("utf-8"), filename="upload_sync.txt")
    # 3. Google Meet
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_ID", "mock-g-id")
    monkeypatch.setenv("GOOGLE_WORKSPACE_CLIENT_SECRET", "mock-g-secret")
    gmeet_p = GoogleMeetProvider()
    class MockGResponse:
        status_code = 200
        text = raw_text
    import requests
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: MockGResponse())
    gmeet_t = gmeet_p.get_transcript("doc-123", access_token="mock-token")
    # 4. Microsoft Teams
    monkeypatch.setenv("AZURE_CLIENT_ID", "mock-a-id")
    monkeypatch.setenv("AZURE_TENANT_ID", "mock-tenant")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "mock-a-secret")
    msteams_p = MSTeamsProvider()
    class MockMResponse:
        status_code = 200
        headers = {"Content-Type": "text/plain"}
        text = raw_text
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: MockMResponse())
    teams_t = msteams_p.get_transcript("m-123/transcripts/t-456", access_token="mock-token")

    mock_llm_json = {
        "project_title": "Refund Dashboard Project",
        "project_overview": {"project_name": "Refund Dashboard", "date": "2026-08-21"},
        "business_objectives": ["Improve customer visibility on refunds"],
        "functional_requirements": [
            {
                "requirement_id": "FR-1",
                "statement": "The system shall provide a customer refund tracking dashboard.",
                "source_evidence": common_evidence,
            }
        ],
        "deferred_or_out_of_scope_items": ["Push notifications parked for v2"],
    }

    # Verify all 4 transcripts flow through the exact same BRD generation logic
    for t in [manual_t, upload_t, gmeet_t, teams_t]:
        assert isinstance(t, NormalizedTranscript)
        assert len(t.raw_text) > 0
        brd = validate_and_create_brd_data(mock_llm_json, t.raw_text)
        assert len(brd.functional_requirements) == 1
        assert brd.functional_requirements[0].requirement_id == "FR-1"
        md = convert_brd_to_markdown(brd)
        assert "## 1. Project Overview" in md
        assert "FR-1" in md



