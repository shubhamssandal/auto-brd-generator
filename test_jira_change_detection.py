"""JIRA-010 tests for deterministic requirement-change reconciliation."""

import pytest

from brd_models import BRDData, NormalizedTranscript, Requirement
from jira_change_detector import (
    apply_approved_changes,
    decide_change,
    detect_jira_changes,
    requirements_for_issue,
    synchronized_baseline,
    validate_changes,
)
from jira_models import ChangeProposal, CreatedIssue, JiraWorkPlan, PlannedIssue, RequirementChange
from jira_service import JiraService
from providers.base import ProviderAPIError


def a_brd():
    return BRDData(
        functional_requirements=[
            Requirement(
                "FR-1",
                "The system shall accept card payments.",
                "we need card payments",
            ),
            Requirement("FR-2", "The system shall email a receipt.", "send a receipt"),
        ]
    )


def a_baseline(key="ENG-10", requirement_id="FR-1"):
    return {
        key: {
            "plan_key": requirement_id,
            "summary": "The system shall accept card payments.",
            "description": "The system shall accept card payments.\n\nAcceptance criteria:\n- FR-1 works.",
            "requirement_ids": (requirement_id,),
            "action_item_ids": (),
        }
    }


def a_created(key="ENG-10", requirement_id="FR-1"):
    return (
        CreatedIssue(
            plan_key=requirement_id,
            issue_key=key,
            source_requirement_ids=(requirement_id,),
        ),
    )


def test_synchronization_baseline_captures_the_exact_issue_content_sent_to_jira():
    plan = JiraWorkPlan(
        project_identifier="ENG",
        issues=(
            PlannedIssue(
                plan_key="FR-1",
                summary="The system shall accept card payments.",
                description="The system shall accept card payments.",
                acceptance_criteria=("FR-1 works.",),
                source_requirement_id="FR-1",
            ),
        ),
    )

    baseline = synchronized_baseline(plan, a_created())

    assert baseline["ENG-10"]["summary"] == "The system shall accept card payments."
    assert baseline["ENG-10"]["description"] == (
        "The system shall accept card payments.\n\nAcceptance criteria:\n\n- FR-1 works."
    )
    assert baseline["ENG-10"]["requirement_ids"] == ("FR-1",)


def test_jira_drift_uses_the_saved_baseline_after_the_plan_changes():
    """A later plan edit must not redefine what Jira was originally sent."""
    baseline = a_baseline()
    created = a_created()

    proposal = detect_jira_changes(
        a_brd(),
        issues=(
            {
                "issue_key": "ENG-10",
                "summary": "The system shall accept card payments.",
                "description": "The system shall accept card and wallet payments.",
            },
        ),
        created=created,
        baseline=baseline,
    )

    assert proposal.changes[0].requirement_id == "FR-1"
    assert proposal.changes[0].proposed_new_text == "The system shall accept card and wallet payments."


def test_jira_summary_drift_creates_one_pending_traceable_change():
    proposal = detect_jira_changes(
        a_brd(),
        issues=(
            {
                "issue_key": "ENG-10",
                "summary": "The system shall accept card and wallet payments.",
                "description": "The system shall accept card payments.",
            },
        ),
        created=a_created(),
        baseline=a_baseline(),
    )

    change = proposal.changes[0]
    assert change.approval_state == "pending"
    assert change.source_type == "jira"
    assert change.requirement_id == "FR-1"
    assert change.jira_issue_key == "ENG-10"
    assert change.affected_issue_keys == ("ENG-10",)
    assert change.old_text == "The system shall accept card payments."
    assert change.proposed_new_text == "The system shall accept card and wallet payments."


def test_jira_adf_description_drift_uses_the_requirement_paragraph():
    proposal = detect_jira_changes(
        a_brd(),
        issues=(
            {
                "issue_key": "ENG-10",
                "summary": "The system shall accept card payments.",
                "description": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "The system shall accept card and wallet payments.",
                                }
                            ],
                        },
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Acceptance criteria:"}],
                        },
                    ],
                },
            },
        ),
        created=a_created(),
        baseline=a_baseline(),
    )

    assert proposal.changes[0].proposed_new_text == "The system shall accept card and wallet payments."
    assert proposal.changes[0].jira_field == "description"


def test_jira_no_drift_reports_no_change():
    proposal = detect_jira_changes(
        a_brd(),
        issues=(
            {
                "issue_key": "ENG-10",
                "summary": "The system shall accept card payments.",
                "description": a_baseline()["ENG-10"]["description"],
            },
        ),
        created=a_created(),
        baseline=a_baseline(),
    )

    assert proposal.changes == ()
    assert "all still match" in proposal.notes[0]


def test_jira_read_failure_is_reported_without_treating_an_unread_issue_as_unchanged():
    proposal = detect_jira_changes(
        a_brd(),
        issues=(),
        created=a_created(),
        baseline=a_baseline(),
        failures=("ENG-10",),
    )

    assert proposal.changes == ()
    assert "could not be read" in proposal.notes[0]


def test_malformed_jira_response_is_safe_and_creates_no_proposal():
    proposal = detect_jira_changes(
        a_brd(),
        issues=object(),
        created=a_created(),
        baseline=a_baseline(),
    )

    assert proposal.changes == ()


@pytest.mark.parametrize(
    ("baseline", "created", "expected"),
    [
        ({}, a_created(), "no recorded baseline"),
        (a_baseline(requirement_id="FR-404"), a_created(), "not linked to a BRD requirement"),
    ],
)
def test_jira_unknown_baseline_or_mapping_is_blocked(baseline, created, expected):
    proposal = detect_jira_changes(
        a_brd(),
        issues=(
            {
                "issue_key": "ENG-10",
                "summary": "Changed requirement",
                "description": "Changed requirement",
            },
        ),
        created=created,
        baseline=baseline,
    )

    assert proposal.changes == ()
    assert expected in " ".join(proposal.notes)


def test_duplicate_jira_entries_do_not_create_duplicate_mutations():
    changed = {
        "issue_key": "ENG-10",
        "summary": "The system shall accept card and wallet payments.",
        "description": "The system shall accept card payments.",
    }

    proposal = detect_jira_changes(
        a_brd(),
        issues=(changed, changed),
        created=a_created(),
        baseline=a_baseline(),
    )

    assert len(proposal.changes) == 1


def test_meeting_validation_covers_new_changed_removed_and_unclear_without_trusting_ids():
    transcript = NormalizedTranscript(
        raw_text="Add wallet payments. Card payments are deferred. Receipt timing is unclear.",
        source="manual",
    )
    proposal = validate_changes(
        (
            {
                "change_type": "NEW",
                "proposed_statement": "The system shall accept wallet payments.",
                "source_evidence": "Add wallet payments.",
            },
            {
                "requirement_id": "FR-1",
                "change_type": "REMOVED_DEFERRED",
                "proposed_statement": "unused",
                "source_evidence": "Card payments are deferred.",
            },
            {
                "requirement_id": "FR-2",
                "change_type": "UNCLEAR",
                "source_evidence": "Receipt timing is unclear.",
            },
        ),
        a_brd(),
        transcript,
    )

    assert [change.change_type for change in proposal.changes] == [
        "NEW",
        "REMOVED_DEFERRED",
        "UNCLEAR",
    ]
    assert proposal.changes[0].requirement_id == "FR-3"
    assert proposal.changes[2].needs_manual_review is True
    assert all(change.approval_state == "pending" for change in proposal.changes)


def test_meeting_validation_blocks_fabricated_evidence_unknown_ids_and_duplicate_changes():
    transcript = NormalizedTranscript(raw_text="Card payments now support wallets.", source="manual")
    proposal = validate_changes(
        (
            {
                "change_id": "C-1",
                "requirement_id": "FR-1",
                "change_type": "CHANGED",
                "proposed_statement": "The system shall accept card and wallet payments.",
                "source_evidence": "Fabricated quote",
            },
            {
                "change_id": "C-2",
                "requirement_id": "FR-1",
                "change_type": "CHANGED",
                "proposed_statement": "The system shall accept card and wallet payments.",
                "source_evidence": "Card payments now support wallets.",
            },
            {
                "requirement_id": "FR-404",
                "change_type": "CHANGED",
                "proposed_statement": "Unknown.",
                "source_evidence": "Card payments now support wallets.",
            },
        ),
        a_brd(),
        transcript,
    )

    assert len(proposal.changes) == 1
    assert proposal.changes[0].needs_manual_review is True
    assert proposal.changes[0].source_evidence == ""
    assert "not in this BRD" in " ".join(proposal.notes)


def test_meeting_changed_requirement_keeps_the_verified_evidence_and_jira_traceability():
    transcript = NormalizedTranscript(
        raw_text="Card payments now support wallets.", source="google_meet", meeting_title="Payments review"
    )
    created = a_created()
    proposal = validate_changes(
        (
            {
                "requirement_id": "FR-1",
                "change_type": "CHANGED",
                "proposed_statement": "The system shall accept card and wallet payments.",
                "source_evidence": "Card payments now support wallets.",
            },
        ),
        a_brd(),
        transcript,
        created=created,
    )

    change = proposal.changes[0]
    assert change.source_type == "google_meet"
    assert change.source_evidence == "Card payments now support wallets."
    assert change.affected_issue_keys == ("ENG-10",)
    assert requirements_for_issue("ENG-10", created) == ("FR-1",)


def test_review_states_only_apply_an_explicit_approved_change_to_its_requirement():
    change = RequirementChange(
        change_id="J-ENG-10",
        source_type="jira",
        change_type="CHANGED",
        requirement_id="FR-1",
        old_text="The system shall accept card payments.",
        proposed_new_text="The system shall accept card and wallet payments.",
    )
    pending = ChangeProposal(source_type="jira", changes=(change,))

    rejected = decide_change(pending, change.change_id, "rejected")
    jira_only = decide_change(pending, change.change_id, "jira_only")
    approved = decide_change(pending, change.change_id, "approved")

    assert apply_approved_changes(a_brd(), pending)[0] == a_brd()
    assert apply_approved_changes(a_brd(), rejected)[0] == a_brd()
    assert apply_approved_changes(a_brd(), jira_only)[0] == a_brd()
    updated, applied = apply_approved_changes(a_brd(), approved)
    assert [item.requirement_id for item in applied] == ["FR-1"]
    assert updated.functional_requirements[0].statement == "The system shall accept card and wallet payments."


def test_approval_cannot_apply_missing_or_stale_requirement_values():
    incomplete = RequirementChange(
        change_id="J-1",
        source_type="jira",
        change_type="CHANGED",
        requirement_id="FR-1",
        old_text="",
        proposed_new_text="Changed.",
    )
    stale = RequirementChange(
        change_id="J-2",
        source_type="jira",
        change_type="CHANGED",
        requirement_id="FR-1",
        old_text="A no longer current statement.",
        proposed_new_text="Changed.",
    )

    assert decide_change(ChangeProposal(changes=(incomplete,)), "J-1", "approved").changes[0].is_pending
    approved = decide_change(ChangeProposal(changes=(stale,)), "J-2", "approved")
    updated, applied = apply_approved_changes(a_brd(), approved)
    assert updated == a_brd()
    assert applied == ()


class FakeJiraResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.payload = payload
        self.text = text

    def json(self):
        return self.payload


def test_existing_jira_field_reader_requests_only_requirement_content(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeJiraResponse(
            payload={
                "key": "ENG-10",
                "fields": {
                    "summary": "Updated requirement",
                    "description": {"type": "doc", "content": []},
                },
            }
        )

    monkeypatch.setattr("jira_service.requests.get", get)
    current = JiraService().get_issue_fields("access-token", "cloud-id", "ENG-10")

    assert current == {
        "issue_key": "ENG-10",
        "summary": "Updated requirement",
        "description": {"type": "doc", "content": []},
    }
    assert calls[0][0].endswith("/cloud-id/rest/api/3/issue/ENG-10")
    assert calls[0][1]["params"] == {"fields": "summary,description"}


def test_jira_field_reader_surfaces_api_failure_without_a_proposal(monkeypatch):
    monkeypatch.setattr(
        "jira_service.requests.get",
        lambda *args, **kwargs: FakeJiraResponse(
            status_code=500, payload={"message": "unavailable"}
        ),
    )

    with pytest.raises(ProviderAPIError, match="HTTP 500"):
        JiraService().get_issue_fields("access-token", "cloud-id", "ENG-10")
