"""
Phase 2: the approved BRD -> PRD stage, from generation through explicit approval.

Three properties are pinned here, because each one is a rule the product direction
states rather than an implementation detail.

*Traceability is structural.* Every PRD feature names the BRD requirement ids it
serves, unknown ids are removed and a feature naming none is dropped. A PRD that
invented product scope would look identical to a correct one otherwise.

*Approval is explicit.* Generating a PRD leaves it pending review, editing it leaves it
pending review, and only the approval control moves it to Approved -- for the BRD gate
in front of it as well as for the PRD itself.

*A failure degrades, it never fabricates.* No BRD, an unapproved BRD, an unreachable
provider and a malformed response each have one defined answer, and none of them is a
PRD that claims coverage it does not have.

The model is never called: ``generate`` is injected as a plain callable, so a test
supplies the exact response text it wants to exercise.
"""

import json
from contextlib import nullcontext
from dataclasses import replace

import pytest
import streamlit as st

import main
from brd_models import BRDData, NormalizedTranscript
from lifecycle_models import (
    APPROVED,
    DISCOVERY_BRD,
    DRAFT,
    NOT_STARTED,
    PENDING_REVIEW,
    PRD,
    lifecycle_from,
)
from prd_generator import (
    PRDResponseError,
    assemble_prd,
    build_baseline_prd,
    build_prd_prompt,
    generate_prd,
    parse_prd_response,
)
from prd_models import PRDData, brd_requirement_ids
from test_jira_sites import (  # noqa: F401 -- fixtures are used by pytest, not by name
    clean_session,
    forbid_write_requests,
)
from test_jira_work_plan import a_brd

REFINEMENT = NormalizedTranscript(
    raw_text="Product call: split the receipt into email and in-app copies.",
    source="google_meet",
)

# A response of the shape the prompt asks for, carrying product detail the BRD does not:
# personas, a journey, behaviour, edge cases and acceptance criteria per feature.
A_RESPONSE = {
    "overview": "A payments experience that gets a shopper from cart to receipt.",
    "goals": ["Fewer abandoned checkouts", "A receipt the shopper trusts"],
    "personas": [
        {
            "name": "Returning shopper",
            "description": "Buys monthly, saves a card.",
            "needs": ["Pay in two taps"],
        }
    ],
    "features": [
        {
            "name": "Card checkout",
            "summary": "Take a card payment at checkout.",
            "requirement_ids": ["FR-1"],
            "behaviours": ["Authorise the card before confirming the order"],
            "edge_cases": ["The issuer declines after the order is drafted"],
            "acceptance_criteria": ["A declined card leaves no confirmed order"],
        },
        {
            "name": "Receipt delivery",
            "summary": "Email a receipt once payment settles.",
            "requirement_ids": ["FR-2", "NFR-1"],
            "behaviours": ["Send the receipt within the response budget"],
            "edge_cases": ["The mailbox bounces"],
            "acceptance_criteria": ["Every settled payment has one receipt"],
        },
    ],
    "user_journeys": [
        {
            "name": "Cart to receipt",
            "persona": "Returning shopper",
            "steps": ["Open cart", "Pay by card", "Read the receipt"],
            "requirement_ids": ["FR-1"],
        }
    ],
    "success_metrics": ["Checkout abandonment below the current rate"],
    "assumptions": ["One currency at launch"],
    "open_questions": ["Which gateway?"],
}


def responder(payload):
    """A ``generate`` callable answering with ``payload`` however it is given."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return lambda prompt: text


def a_prd(**overrides) -> PRDData:
    """The PRD a good response assembles to, so UI tests start from a real artifact."""
    prd = generate_prd(a_brd(), None, generate=responder(A_RESPONSE))
    return replace(prd, **overrides) if overrides else prd


# --- Generation from an approved BRD --------------------------------------


def test_an_approved_brd_generates_a_prd_that_adds_product_detail():
    brd = a_brd()

    prd = generate_prd(brd, None, generate=responder(A_RESPONSE))

    assert prd.is_baseline is False
    assert prd.notes == ()
    assert prd.project_title == brd.project_title
    assert prd.source_requirement_ids == ("FR-1", "FR-2", "NFR-1")
    assert [feature.feature_id for feature in prd.features] == ["F-1", "F-2"]
    # The detail a BRD requirement does not carry, which is the point of the stage.
    assert prd.overview and prd.goals and prd.personas
    assert prd.journeys[0].steps == ("Open cart", "Pay by card", "Read the receipt")
    assert all(feature.behaviours for feature in prd.features)
    assert all(feature.edge_cases for feature in prd.features)
    assert all(feature.acceptance_criteria for feature in prd.features)
    assert prd.success_metrics and prd.assumptions and prd.open_questions
    # Not a restatement: no feature merely repeats a BRD requirement statement.
    statements = {r.statement for r in brd.functional_requirements}
    assert not statements & {feature.name for feature in prd.features}


def test_the_prd_stage_reaches_pending_review_and_not_approved():
    brd = a_brd()
    prd = generate_prd(brd, None, generate=responder(A_RESPONSE))

    lifecycle = lifecycle_from(brd=brd, brd_approved=True, prd=prd)

    state = lifecycle.state(PRD)
    assert state.status == PENDING_REVIEW
    assert "2 feature(s) covering 3 of 3 BRD requirement(s)" in state.detail
    assert lifecycle.prd is prd


# --- No BRD, or a BRD nobody approved -------------------------------------


def test_without_a_brd_the_prd_is_empty_and_says_why():
    prd = generate_prd(None, None, generate=responder(A_RESPONSE))

    assert prd.is_empty
    assert prd.features == ()
    assert "needs an approved BRD" in prd.notes[0]


def test_a_brd_with_no_identified_requirement_generates_nothing():
    brd = a_brd(functional_requirements=[], non_functional_requirements=[])

    prd = generate_prd(brd, None, generate=responder(A_RESPONSE))

    assert prd.is_empty
    assert brd_requirement_ids(brd) == ()


@pytest.mark.parametrize(
    "brd_approved, expected_fragment",
    [(False, "Approve it to generate"), (True, "Generate a PRD from it")],
    ids=["unapproved", "approved"],
)
def test_the_stage_gates_on_the_brd_being_approved(brd_approved, expected_fragment):
    lifecycle = lifecycle_from(brd=a_brd(), brd_approved=brd_approved)

    state = lifecycle.state(PRD)
    assert state.status == NOT_STARTED
    assert expected_fragment in state.detail


def test_an_unapproved_brd_offers_no_generation_and_calls_no_model(monkeypatch):
    shown = render_prd_stage(monkeypatch, brd_approved=False, clicked={"generate_prd"})

    assert any("pending review" in text for text in shown["info"])
    assert main.PRD_SESSION_KEY not in st.session_state


# --- The optional refinement transcript ------------------------------------


def test_the_refinement_transcript_is_optional_and_recorded_when_given():
    brd = a_brd()

    without = generate_prd(brd, None, generate=responder(A_RESPONSE))
    with_transcript = generate_prd(brd, REFINEMENT, generate=responder(A_RESPONSE))

    assert without.refinement_source == ""
    assert with_transcript.refinement_source == "google_meet"
    # The BRD alone is sufficient: the same features are produced either way.
    assert [f.name for f in without.features] == [f.name for f in with_transcript.features]


def test_the_prompt_carries_the_brd_and_only_quotes_a_refinement_it_was_given():
    brd = a_brd()

    plain = build_prd_prompt(brd)
    enriched = build_prd_prompt(brd, REFINEMENT)

    assert "FR-1" in plain and "The system shall accept card payments." in plain
    assert "split the receipt" not in plain
    assert "split the receipt" in enriched
    assert "supporting evidence only" in enriched


# --- Traceability back to the BRD -----------------------------------------


def test_a_feature_naming_no_brd_requirement_is_dropped():
    payload = {
        "features": [
            {"name": "Loyalty points", "requirement_ids": []},
            {"name": "Card checkout", "requirement_ids": ["FR-1"]},
        ]
    }

    prd = assemble_prd(payload, a_brd())

    assert [feature.name for feature in prd.features] == ["Card checkout"]
    assert any("named no BRD requirement" in note for note in prd.notes)


def test_a_requirement_id_the_brd_does_not_hold_is_removed():
    payload = {
        "features": [{"name": "Card checkout", "requirement_ids": ["FR-1", "FR-99"]}]
    }

    prd = assemble_prd(payload, a_brd())

    assert prd.features[0].requirement_ids == ("FR-1",)
    assert any("not in the BRD" in note for note in prd.notes)


def test_uncovered_requirements_are_reported_rather_than_hidden():
    payload = {"features": [{"name": "Card checkout", "requirement_ids": ["FR-1"]}]}

    prd = assemble_prd(payload, a_brd())

    assert prd.covered_requirement_ids == ("FR-1",)
    assert prd.uncovered_requirement_ids == ("FR-2", "NFR-1")
    assert any("not covered by any feature" in note for note in prd.notes)


def test_every_feature_traces_and_the_baseline_traces_one_to_one():
    brd = a_brd()

    baseline = build_baseline_prd(brd)

    assert baseline.is_baseline is True
    assert baseline.uncovered_requirement_ids == ()
    assert all(feature.is_traceable for feature in baseline.features)
    assert [f.requirement_ids for f in baseline.features] == [("FR-1",), ("FR-2",), ("NFR-1",)]
    # A fallback is a draft, not something to review as a product definition.
    assert lifecycle_from(brd=brd, brd_approved=True, prd=baseline).state(PRD).status == DRAFT


# --- Provider and malformed-response failures ------------------------------


def test_a_provider_failure_falls_back_and_leaks_no_client_detail():
    def failing(prompt):
        raise RuntimeError("401 token=secret-value for https://internal/api")

    prd = generate_prd(a_brd(), None, generate=failing)

    assert prd.is_baseline is True
    assert prd.features, "the reviewer still gets a traceable starting point"
    assert "RuntimeError" in prd.notes[0]
    assert "secret-value" not in " ".join(prd.notes)
    assert "internal" not in " ".join(prd.notes)


@pytest.mark.parametrize(
    "raw",
    ["", "not json at all", "[]", json.dumps({"overview": "no features here"})],
    ids=["empty", "prose", "empty-list", "no-features"],
)
def test_a_malformed_response_falls_back_instead_of_raising(raw):
    prd = generate_prd(a_brd(), None, generate=responder(raw))

    assert prd.is_baseline is True
    assert prd.notes and prd.notes[0]
    assert prd.features


def test_the_reader_refuses_a_response_that_is_not_a_prd():
    for raw in ("", "not json", json.dumps({"overview": "x"})):
        with pytest.raises(PRDResponseError):
            parse_prd_response(raw)


def test_no_model_configured_still_produces_a_traceable_draft():
    prd = generate_prd(a_brd(), None, generate=None)

    assert prd.is_baseline is True
    assert "was not available" in prd.notes[0]
    assert len(prd.features) == 3


def test_a_response_holding_only_untraceable_features_falls_back():
    payload = {"features": [{"name": "Loyalty points", "requirement_ids": ["FR-99"]}]}

    prd = generate_prd(a_brd(), None, generate=responder(payload))

    assert prd.is_baseline is True
    assert any("no feature that traced to the BRD" in note for note in prd.notes)


# --- The stage in main: generate, review, edit, approve --------------------


def render_prd_stage(monkeypatch, brd_approved=True, prd=None, values=None, clicked=(),
                     response=A_RESPONSE, brd=None):
    """
    Render the PRD stage over the session state a real run would arrive with.

    Every widget is replaced with a recorder, so an untouched field returns its stored
    value and ``values`` names the one field a reviewer changed. ``clicked`` names the
    buttons pressed by key: nothing is generated or approved unless a test presses it.
    """
    brd = brd if brd is not None else a_brd()
    st.session_state[main.BRD_SESSION_KEY] = brd
    if brd_approved:
        st.session_state[main.BRD_APPROVED_SESSION_KEY] = True
    if prd is not None:
        st.session_state[main.PRD_SESSION_KEY] = prd

    values = dict(values or {})
    shown = {
        key: [] for key in ("success", "warning", "error", "info", "caption", "markdown")
    }
    for widget in shown:
        monkeypatch.setattr(
            main.st, widget, lambda text, *a, _w=widget, **k: shown[_w].append(str(text))
        )
    monkeypatch.setattr(
        main.st, "text_area", lambda label, value="", key=None, **k: values.get(key, value)
    )
    monkeypatch.setattr(
        main.st, "text_input", lambda label, value="", key=None, **k: values.get(key, value)
    )
    monkeypatch.setattr(
        main.st, "checkbox", lambda label, value=False, key=None, **k: values.get(key, value)
    )
    monkeypatch.setattr(main.st, "button", lambda label, **k: k.get("key") in clicked)
    monkeypatch.setattr(main.st, "spinner", lambda *a, **k: nullcontext())
    monkeypatch.setattr(main, "_planner_generate", lambda: responder(response))

    lifecycle = lifecycle_from(
        brd=brd,
        brd_approved=brd_approved,
        prd=st.session_state.get(main.PRD_SESSION_KEY),
        prd_approved=bool(st.session_state.get(main.PRD_APPROVED_SESSION_KEY)),
    )
    main._render_prd_stage(lifecycle)
    return shown


def stored_prd() -> PRDData:
    return st.session_state[main.PRD_SESSION_KEY]


def test_pressing_generate_stores_a_prd_and_approves_nothing(monkeypatch):
    render_prd_stage(monkeypatch, clicked={"generate_prd"})

    prd = stored_prd()
    assert len(prd.features) == 2
    assert st.session_state.get(main.PRD_APPROVED_SESSION_KEY) is None


def test_nothing_is_generated_until_the_button_is_pressed(monkeypatch):
    shown = render_prd_stage(monkeypatch, clicked=())

    assert main.PRD_SESSION_KEY not in st.session_state
    assert any("Not generated yet" in text for text in shown["caption"])


def test_an_edit_is_kept_and_leaves_the_prd_pending_review(monkeypatch):
    prd = a_prd()
    feature_key = main._PRD_WIDGET_PREFIX + "F-1_criteria"

    render_prd_stage(
        monkeypatch,
        prd=prd,
        values={
            main._PRD_WIDGET_PREFIX + "overview": "A reviewer's own overview.",
            feature_key: "A declined card leaves no confirmed order\nThe cart survives",
        },
    )

    edited = stored_prd()
    assert edited.overview == "A reviewer's own overview."
    assert edited.features[0].acceptance_criteria == (
        "A declined card leaves no confirmed order",
        "The cart survives",
    )
    # Editing does not approve, and it does not touch traceability.
    assert st.session_state.get(main.PRD_APPROVED_SESSION_KEY) is None
    assert edited.features[0].requirement_ids == prd.features[0].requirement_ids
    assert (
        lifecycle_from(brd=a_brd(), brd_approved=True, prd=edited).state(PRD).status
        == PENDING_REVIEW
    )


def test_an_edit_cannot_rewrite_which_brd_requirement_a_feature_serves(monkeypatch):
    prd = a_prd()

    render_prd_stage(
        monkeypatch,
        prd=prd,
        values={main._PRD_WIDGET_PREFIX + "F-1_name": "Card checkout, revised"},
    )

    edited = stored_prd()
    assert edited.features[0].name == "Card checkout, revised"
    assert edited.source_requirement_ids == prd.source_requirement_ids
    assert [f.requirement_ids for f in edited.features] == [
        f.requirement_ids for f in prd.features
    ]


def test_the_prd_is_approved_only_when_the_approval_button_is_pressed(monkeypatch):
    prd = a_prd()

    render_prd_stage(monkeypatch, prd=prd)
    assert st.session_state.get(main.PRD_APPROVED_SESSION_KEY) is None

    render_prd_stage(monkeypatch, prd=prd, clicked={"approve_prd"})

    assert st.session_state[main.PRD_APPROVED_SESSION_KEY] is True
    assert (
        lifecycle_from(brd=a_brd(), brd_approved=True, prd=prd, prd_approved=True)
        .state(PRD)
        .status
        == APPROVED
    )


def test_an_approved_prd_is_shown_read_only_until_approval_is_revoked(monkeypatch):
    prd = a_prd()
    st.session_state[main.PRD_APPROVED_SESSION_KEY] = True

    shown = render_prd_stage(
        monkeypatch,
        prd=prd,
        values={main._PRD_WIDGET_PREFIX + "overview": "An edit that must not land."},
    )

    assert any("approved" in text for text in shown["success"])
    assert stored_prd().overview == prd.overview

    render_prd_stage(monkeypatch, prd=prd, clicked={"revoke_prd_approval"})
    assert st.session_state[main.PRD_APPROVED_SESSION_KEY] is False


def test_the_brd_is_approved_only_by_its_own_control(monkeypatch):
    monkeypatch.setattr(main.st, "caption", lambda text, **k: None)
    monkeypatch.setattr(main.st, "success", lambda text, **k: None)
    main._store_brd(a_brd(), "manual")

    monkeypatch.setattr(main.st, "button", lambda label, **k: False)
    main._render_brd_approval()
    assert main._brd_approved() is False

    monkeypatch.setattr(main.st, "button", lambda label, **k: k.get("key") == "approve_brd")
    main._render_brd_approval()
    assert main._brd_approved() is True
    assert lifecycle_from(brd=a_brd(), brd_approved=True).state(DISCOVERY_BRD).status == APPROVED


def test_a_new_brd_invalidates_the_approval_and_the_prd_derived_from_it():
    st.session_state[main.BRD_APPROVED_SESSION_KEY] = True
    st.session_state[main.PRD_SESSION_KEY] = a_prd()
    st.session_state[main.PRD_APPROVED_SESSION_KEY] = True
    st.session_state[main._PRD_WIDGET_PREFIX + "overview"] = "stale widget text"

    main._store_brd(a_brd(project_title="A later discovery call"), "microsoft_teams")

    assert main._brd_approved() is False
    assert main.PRD_SESSION_KEY not in st.session_state
    assert main.PRD_APPROVED_SESSION_KEY not in st.session_state
    assert main._PRD_WIDGET_PREFIX + "overview" not in st.session_state
    # The new BRD itself is intact, and it is pending review again.
    assert isinstance(st.session_state[main.BRD_SESSION_KEY], BRDData)
    assert (
        lifecycle_from(brd=st.session_state[main.BRD_SESSION_KEY]).state(DISCOVERY_BRD).status
        == PENDING_REVIEW
    )


def test_regenerating_drops_the_previous_approval_and_editor_state(monkeypatch):
    st.session_state[main.PRD_APPROVED_SESSION_KEY] = True
    st.session_state[main._PRD_WIDGET_PREFIX + "overview"] = "stale widget text"

    render_prd_stage(monkeypatch, prd=a_prd(), clicked={"generate_prd"})

    assert main.PRD_APPROVED_SESSION_KEY not in st.session_state
    assert main._PRD_WIDGET_PREFIX + "overview" not in st.session_state
    assert len(stored_prd().features) == 2


def test_a_generation_failure_in_the_stage_is_shown_not_raised(monkeypatch):
    shown = render_prd_stage(
        monkeypatch, clicked={"generate_prd"}, response="not a PRD at all"
    )

    prd = stored_prd()
    assert prd.is_baseline is True
    assert any("maps each BRD requirement to one feature" in text for text in shown["warning"])
    assert st.session_state.get(main.PRD_APPROVED_SESSION_KEY) is None
