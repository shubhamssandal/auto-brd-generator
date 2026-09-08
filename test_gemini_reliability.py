import pytest
import logging
from unittest.mock import MagicMock, patch
from main import _gemini_json, CLIENT, GEMINI_MODEL
from google.api_core.exceptions import ServiceUnavailable, InternalServerError, PermissionDenied
from google.genai import types

logger = logging.getLogger("main")

def test_gemini_json_success():
    """Verify that a successful Gemini API request returns normally."""
    mock_response = MagicMock()
    mock_response.text = '{"project_title": "Test Project"}'

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    with patch("main.CLIENT", mock_client):
        result = _gemini_json("Some prompt")
        assert result == '{"project_title": "Test Project"}'
        mock_client.models.generate_content.assert_called_once_with(
            model=GEMINI_MODEL,
            contents="Some prompt",
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )

def test_gemini_json_transient_retry_success():
    """Verify that a transient error (e.g., ServiceUnavailable) triggers a retry and succeeds."""
    mock_response = MagicMock()
    mock_response.text = '{"project_title": "Success After Retry"}'

    mock_client = MagicMock()
    # First call raises ServiceUnavailable, second call succeeds
    mock_client.models.generate_content.side_effect = [
        ServiceUnavailable("Service is temporarily unavailable"),
        mock_response
    ]

    with patch("main.CLIENT", mock_client), patch("main.time.sleep") as mock_sleep:
        result = _gemini_json("Some prompt")
        assert result == '{"project_title": "Success After Retry"}'
        assert mock_client.models.generate_content.call_count == 2
        mock_sleep.assert_called_once()
        # Verify first retry sleep delay was approximately 1.0s (exponential backoff base)
        args, _ = mock_sleep.call_args
        assert 1.0 <= args[0] <= 1.1

def test_gemini_json_transient_exhaustion():
    """Verify that repeated transient errors eventually raise the exception after retries are exhausted."""
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = [
        ServiceUnavailable("503 error"),
        InternalServerError("500 error"),
        ServiceUnavailable("503 error"),
        ServiceUnavailable("503 error")
    ]

    with patch("main.CLIENT", mock_client), patch("main.time.sleep") as mock_sleep, pytest.raises(ServiceUnavailable):
        _gemini_json("Some prompt")

    assert mock_client.models.generate_content.call_count == 4  # Initial + 3 retries
    assert mock_sleep.call_count == 3

def test_gemini_json_permanent_error_no_retry():
    """Verify that a permanent error (e.g., PermissionDenied 403) raises immediately without retrying."""
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = PermissionDenied("403 Forbidden")

    with patch("main.CLIENT", mock_client), patch("main.time.sleep") as mock_sleep, pytest.raises(PermissionDenied):
        _gemini_json("Some prompt")

    assert mock_client.models.generate_content.call_count == 1
    mock_sleep.assert_not_called()

SAMPLE_BRD_JSON = """{
  "project_title": "Payment Project",
  "project_overview": {
    "project_name": "Payment Engine",
    "date": "2026-09-08"
  },
  "business_objectives": ["Process payments securely"],
  "stakeholders": [],
  "key_decisions": [],
  "functional_requirements": [
    {
      "requirement_id": "FR-1",
      "statement": "The system shall handle payment processing.",
      "source_evidence": "The system shall handle payment processing."
    }
  ],
  "non_functional_requirements": [],
  "acceptance_criteria": [],
  "dependencies": [],
  "risks": [],
  "assumptions": [],
  "open_questions": [],
  "deferred_or_out_of_scope_items": [],
  "action_items": [],
  "background_information": []
}"""

SAMPLE_NOTES = "The system shall handle payment processing."

def test_generate_brd_from_notes_uses_retry_and_succeeds():
    """Verify BRD generation calls Gemini via retry logic and returns valid BRDData."""
    from main import generate_brd_from_notes

    mock_response = MagicMock()
    mock_response.text = SAMPLE_BRD_JSON

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    with patch("main.CLIENT", mock_client):
        brd = generate_brd_from_notes(SAMPLE_NOTES)
        assert brd.project_title == "Payment Project"
        assert len(brd.functional_requirements) == 1
        assert brd.functional_requirements[0].statement == "The system shall handle payment processing."
        mock_client.models.generate_content.assert_called_once()

def test_generate_brd_from_notes_transient_503_retry_success():
    """Verify 503 during BRD generation triggers retry and ultimately succeeds."""
    from main import generate_brd_from_notes

    mock_response = MagicMock()
    mock_response.text = SAMPLE_BRD_JSON

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = [
        ServiceUnavailable("503 UNAVAILABLE: High demand"),
        mock_response
    ]

    with patch("main.CLIENT", mock_client), patch("main.time.sleep") as mock_sleep:
        brd = generate_brd_from_notes(SAMPLE_NOTES)
        assert brd.project_title == "Payment Project"
        assert mock_client.models.generate_content.call_count == 2
        mock_sleep.assert_called_once()

def test_generate_brd_from_notes_transient_exhaustion():
    """Verify repeated 503 during BRD generation raises exception after retries are exhausted."""
    from main import generate_brd_from_notes

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = [
        ServiceUnavailable("503 error 1"),
        ServiceUnavailable("503 error 2"),
        ServiceUnavailable("503 error 3"),
        ServiceUnavailable("503 error 4")
    ]

    with patch("main.CLIENT", mock_client), patch("main.time.sleep") as mock_sleep, pytest.raises(ServiceUnavailable):
        generate_brd_from_notes(SAMPLE_NOTES)

    assert mock_client.models.generate_content.call_count == 4
    assert mock_sleep.call_count == 3

def test_generate_brd_from_notes_permanent_error_no_retry():
    """Verify permanent error (PermissionDenied) during BRD generation raises immediately."""
    from main import generate_brd_from_notes

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = PermissionDenied("403 Forbidden")

    with patch("main.CLIENT", mock_client), patch("main.time.sleep") as mock_sleep, pytest.raises(PermissionDenied):
        generate_brd_from_notes(SAMPLE_NOTES)

    assert mock_client.models.generate_content.call_count == 1
    mock_sleep.assert_not_called()
