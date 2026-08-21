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


def test_validate_and_create_brd_data_complete_sections(sample_notes):
    """
    Tests extraction and mapping of all structured BRD sections:
    decisions, acceptance criteria, dependencies, risks, and action items.
    """
    mock_data = {
        "project_title": "Refund Notification System",
        "project_overview": {"project_name": "Refund Notifications", "date": "Aug 18"},
        "business_objectives": ["Reduce refund status support tickets by 30%"],
        "stakeholders": [
            {"name": "Priya", "role": "PM", "requirement": "Email notification for refund status changes"},
            "Sarah (Support)",
        ],
        "key_decisions": ["Email notifications are in scope for v1; push notifications are deferred"],
        "functional_requirements": [
            {
                "requirement_id": "FR-1",
                "statement": "The system shall send an email notification when refund status changes.",
                "source_evidence": "Priya wants a notification when refund status changes - email at \nleast",
            }
        ],
        "non_functional_requirements": [],
        "acceptance_criteria": ["Customer receives email within 5 minutes of refund status change"],
        "dependencies": ["Old payments DB and new payments DB sync"],
        "risks": ["Data duplication between payments databases"],
        "assumptions": [{"statement": "Old DB schema is accessible", "confirmation_needed": "Confirm with Rahul"}],
        "open_questions": ["Who owns QA before release?"],
        "deferred_or_out_of_scope_items": ["Push notifications", "Showing refund status in agent tool"],
        "action_items": [
            {"item": "Spike payments DB duplication", "owner": "Rahul", "due_date": "Aug 25"},
            "Follow up on QA ownership with Priya",
        ],
        "background_information": ["Refund support tickets account for 30% of total volume"],
    }

    brd = validate_and_create_brd_data(mock_data, sample_notes)

    assert brd.project_title == "Refund Notification System"
    assert brd.project_overview.project_name == "Refund Notifications"
    assert len(brd.business_objectives) == 1
    assert len(brd.stakeholders) == 2
    assert brd.stakeholders[0].name == "Priya"
    assert brd.stakeholders[1].name == "Sarah (Support)"
    assert len(brd.key_decisions) == 1
    assert len(brd.functional_requirements) == 1
    assert len(brd.acceptance_criteria) == 1
    assert len(brd.dependencies) == 1
    assert len(brd.risks) == 1
    assert len(brd.action_items) == 2
    assert brd.action_items[0].owner == "Rahul"
    assert brd.action_items[1].item == "Follow up on QA ownership with Priya"


def test_convert_brd_to_markdown_formatting(sample_notes):
    """Tests markdown output contains all sections and headers."""
    from main import convert_brd_to_markdown

    mock_data = {
        "project_title": "Refund Notification System",
        "project_overview": {"project_name": "Refund Notifications", "date": "Aug 18"},
        "business_objectives": ["Reduce support ticket volume"],
        "stakeholders": [{"name": "Priya", "role": "PM"}],
        "key_decisions": ["Email notifications selected for v1"],
        "functional_requirements": [
            {
                "requirement_id": "FR-1",
                "statement": "The system shall send email notifications.",
                "source_evidence": "Priya wants a notification when refund status changes - email at \nleast",
            }
        ],
        "non_functional_requirements": [],
        "acceptance_criteria": ["Email arrives in inbox"],
        "dependencies": ["Payments DB"],
        "risks": ["DB sync latency"],
        "assumptions": [],
        "open_questions": ["Who owns QA?"],
        "deferred_or_out_of_scope_items": ["Push notifications"],
        "action_items": [{"item": "Spike DB sync", "owner": "Rahul", "due_date": "Aug 25"}],
        "background_information": ["30% ticket volume"],
    }

    brd = validate_and_create_brd_data(mock_data, sample_notes)
    md = convert_brd_to_markdown(brd)

    assert "# Refund Notification System" in md
    assert "## 1. Project Overview" in md
    assert "## 2. Business Objectives" in md
    assert "## 3. Stakeholders" in md
    assert "## 4. Key Decisions" in md
    assert "## 5. Scope" in md
    assert "## 6. Acceptance Criteria" in md
    assert "## 7. Dependencies & Risks" in md
    assert "## 10. Action Items & Next Steps" in md
    assert "- [ ] Spike DB sync (Owner: Rahul) [Due: Aug 25]" in md