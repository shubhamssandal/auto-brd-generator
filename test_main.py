from main import validate_and_create_brd_data
import pytest


@pytest.fixture
def sample_notes():
    """Provides the content of the sample meeting notes."""
    with open("sample_meeting_notes.txt", "r") as f:
        return f.read()


def test_evidence_validation_and_reclassification(sample_notes):
    """
    Tests that a requirement with valid evidence is kept, and one with
    invalid evidence is reclassified as an assumption.
    """
    # This is a fake response from Gemini, simulating its output
    mock_gemini_data = {
        "functional_requirements": [
            {
                "requirement_id": "FR-VALID",
                "statement": "The system shall send an email notification.",
                # This evidence IS in sample_meeting_notes.txt and includes the line break
                "source_evidence": "Priya wants a notification when refund status changes - email at \nleast",
            },
            {
                "requirement_id": "FR-INVALID",
                "statement": "The system should have a blue button.",
                # This evidence IS NOT in sample_meeting_notes.txt
                "source_evidence": "The user wants a blue button.",
            },
        ],
        "assumptions": [],
    }

    # Run the validation function with the mock data and real notes
    brd_data = validate_and_create_brd_data(mock_gemini_data, sample_notes)

    # --- Assertions ---

    # 1. Check that there is exactly one confirmed functional requirement.
    assert len(brd_data.functional_requirements) == 1

    # 2. Check that the confirmed requirement is the one with valid evidence.
    assert brd_data.functional_requirements[0].requirement_id == "FR-VALID"

    # 3. Check that there is exactly one assumption.
    assert len(brd_data.assumptions) == 1

    # 4. Check that the assumption is the re-classified invalid requirement.
    reclassified_assumption = brd_data.assumptions[0]
    assert "FR-INVALID" not in [
        req.requirement_id for req in brd_data.functional_requirements
    ]
    assert (
        "A functional requirement was suggested but its evidence could not be verified"
        in reclassified_assumption.statement
    )
    assert "'The system should have a blue button.'" in reclassified_assumption.statement