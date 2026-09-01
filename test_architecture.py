"""
Phase 3: the approved PRD -> architecture stage, from generation through explicit approval.

Three properties are pinned here, each one a rule rather than an implementation detail.

*The approved PRD is the gate.* No PRD, an empty PRD and an unapproved PRD each block
generation and say what is missing, because an architecture designed against product
behaviour nobody signed off would have to be redesigned.

*Traceability is structural, and cross-cutting design survives it.* A component may only
name feature ids the PRD actually minted, unknown ids are removed, and a component naming
none is *kept* and counted -- unlike a PRD feature, which is an invention if it traces to
nothing. A gateway or shared auth service is real design and dropping it would delete it.

*A failure degrades, it never fabricates.* An unreachable provider and a malformed
response both fall back to the deterministic per-feature skeleton, flagged as a fallback,
and neither one approves anything.

The model is never called: ``generate`` is injected as a plain callable, so a test
supplies the exact response text it wants to exercise.
"""

import json
from contextlib import nullcontext
from dataclasses import replace

import pytest
import streamlit as st

import main
from architecture_generator import (
    ArchitectureResponseError,
    assemble_architecture,
    build_architecture_prompt,
    build_baseline_architecture,
    generate_architecture,
    parse_architecture_response,
)
from architecture_models import (
    BACKEND,
    LAYERS,
    MOBILE,
    WEB,
    ArchitectureData,
    prd_feature_ids,
)
from brd_models import NormalizedTranscript
from lifecycle_models import (
    APPROVED,
    ARCHITECTURE,
    DRAFT,
    NOT_STARTED,
    PENDING_REVIEW,
    lifecycle_from,
)
from prd_models import PRDData
from test_jira_sites import (  # noqa: F401 -- fixtures are used by pytest, not by name
    clean_session,
    forbid_write_requests,
)
from test_jira_work_plan import a_brd
# The PRD these tests design against is the one the PRD stage actually produces, not a
# fixture shaped to suit the architecture.
from test_prd import a_prd

DISCUSSION = NormalizedTranscript(
    raw_text="Design call: the mobile client talks to the same API as the web client.",
    source="microsoft_teams",
)

# A response of the shape the prompt asks for: three layers, an untraced cross-cutting
# component, a decision, a flow, an integration and a risk.
A_RESPONSE = {
    "overview": "A payments platform with one API behind a web and a mobile client.",
    "domains": ["Payments", "Orders"],
    "authentication": ["OIDC access tokens, refreshed by the clients"],
    "backend": [
        {
            "name": "Payment service",
            "responsibility": "Authorise and settle card payments.",
            "feature_ids": ["F-1"],
            "apis": ["POST /payments"],
            "data": ["payments", "payment_attempts"],
            "dependencies": ["Card gateway"],
        },
        {
            "name": "Receipt service",
            "responsibility": "Render and send receipts.",
            "feature_ids": ["F-2"],
            "apis": ["POST /receipts"],
        },
        {
            "name": "API gateway",
            "responsibility": "Terminate TLS and route to the services.",
            "apis": ["/api/*"],
        },
    ],
    "web": [
        {
            "name": "Checkout pages",
            "responsibility": "Collect card details and show the outcome.",
            "feature_ids": ["F-1"],
            "apis": ["POST /payments"],
        }
    ],
    "mobile": [
        {
            "name": "Checkout screens",
            "responsibility": "Collect card details on device.",
            "feature_ids": ["F-1", "F-2"],
        }
    ],
    "data_flows": [
        {
            "name": "Card payment",
            "steps": ["Client posts the card", "Service authorises", "Receipt is queued"],
            "feature_ids": ["F-1"],
        }
    ],
    "integrations": [
        {
            "name": "Card gateway",
            "purpose": "Authorise and capture cards.",
            "direction": "outbound",
            "feature_ids": ["F-1"],
        }
    ],
    "decisions": [
        {
            "title": "One API for both clients",
            "choice": "A single REST API",
            "rationale": "The clients need the same behaviour.",
            "feature_ids": ["F-1"],
        }
    ],
    "dependencies": ["A PCI-compliant card gateway"],
    "risks": [
        {
            "statement": "Gateway latency blocks checkout",
            "impact": "Shoppers abandon the cart",
            "mitigation": "Time out and retry asynchronously",
        }
    ],
}


def responder(payload):
    """A ``generate`` callable answering with ``payload`` however it is given."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return lambda prompt: text


def an_architecture(**overrides) -> ArchitectureData:
    """The architecture a good response assembles to, so UI tests start from a real one."""
    architecture = generate_architecture(a_prd(), None, generate=responder(A_RESPONSE))
    return replace(architecture, **overrides) if overrides else architecture


# --- Generation from an approved PRD --------------------------------------


def test_an_approved_prd_generates_an_architecture_across_the_three_layers():
    prd = a_prd()

    architecture = generate_architecture(prd, None, generate=responder(A_RESPONSE))

    assert architecture.is_baseline is False
    assert architecture.missing_layers == ()
    assert [item.name for item in architecture.backend] == [
        "Payment service",
        "Receipt service",
        "API gateway",
    ]
    assert [item.name for item in architecture.web] == ["Checkout pages"]
    assert [item.name for item in architecture.mobile] == ["Checkout screens"]
    # Design the PRD does not carry: boundaries, ownership, decisions, flows, risks.
    assert architecture.backend[0].apis == ("POST /payments",)
    assert architecture.backend[0].data == ("payments", "payment_attempts")
    assert architecture.decisions[0].decision_id == "AD-1"
    assert architecture.flows[0].steps[0] == "Client posts the card"
    assert architecture.integrations[0].direction == "outbound"
    assert architecture.risks[0].mitigation == "Time out and retry asynchronously"
    assert architecture.auth_approach and architecture.domains
    assert architecture.dependencies == ("A PCI-compliant card gateway",)
    assert architecture.source_prd_title == prd.project_title


def test_component_ids_are_minted_here_and_layers_come_from_the_section():
    """A model-asserted layer cannot override the section a component arrived in."""
    payload = dict(A_RESPONSE)
    payload["web"] = [{"name": "Checkout pages", "layer": "backend", "feature_ids": ["F-1"]}]

    architecture = assemble_architecture(payload, a_prd())

    ids = [item.component_id for item in architecture.components]
    assert ids == ["C-{}".format(position) for position in range(1, len(ids) + 1)]
    assert [item.name for item in architecture.web] == ["Checkout pages"]


def test_the_prompt_carries_the_prd_and_asks_for_all_three_layers():
    prompt = build_architecture_prompt(a_prd())

    assert "F-1" in prompt and "F-2" in prompt
    for word in ("backend", "web", "mobile"):
        assert word in prompt.lower()
    assert "Return JSON only" in prompt


# --- The PRD gate ---------------------------------------------------------


@pytest.mark.parametrize(
    "prd",
    [None, PRDData(), PRDData(project_title="Payments")],
    ids=["no-prd", "empty-prd", "prd-without-features"],
)
def test_without_a_prd_holding_features_nothing_is_designed(prd):
    architecture = generate_architecture(prd, None, generate=responder(A_RESPONSE))

    assert architecture.is_empty
    assert architecture.components == ()
    assert "approved PRD" in architecture.notes[0]


def test_the_stage_is_blocked_until_the_prd_is_approved():
    prd = a_prd()

    unapproved = lifecycle_from(brd=a_brd(), brd_approved=True, prd=prd, prd_approved=False)
    assert unapproved.state(ARCHITECTURE).status == NOT_STARTED
    assert "pending review" in unapproved.state(ARCHITECTURE).detail

    ready = lifecycle_from(brd=a_brd(), brd_approved=True, prd=prd, prd_approved=True)
    assert ready.state(ARCHITECTURE).status == NOT_STARTED
    assert "Generate an architecture" in ready.state(ARCHITECTURE).detail


def test_revoking_the_brd_approval_reopens_the_architecture_stage():
    """The PRD returns to Not Started, so the architecture behind it cannot read as ready."""
    lifecycle = lifecycle_from(
        brd=a_brd(),
        brd_approved=False,
        prd=a_prd(),
        prd_approved=True,
        architecture=an_architecture(),
        architecture_approved=True,
    )

    assert lifecycle.state(ARCHITECTURE).status == NOT_STARTED
    assert lifecycle.state(ARCHITECTURE).status != APPROVED


# --- Traceability ---------------------------------------------------------


def test_every_component_traces_to_a_prd_feature_or_is_kept_as_cross_cutting():
    prd = a_prd()

    architecture = generate_architecture(prd, None, generate=responder(A_RESPONSE))

    assert prd_feature_ids(prd) == ("F-1", "F-2")
    for component in architecture.components:
        for identifier in component.feature_ids:
            assert identifier in prd_feature_ids(prd)
    gateway = [item for item in architecture.components if item.name == "API gateway"][0]
    assert gateway.is_traceable is False
    # Kept, and reported -- not dropped, because a gateway is real design.
    assert any("cross-cutting" in note for note in architecture.notes)
    assert architecture.covered_feature_ids == ("F-1", "F-2")
    assert architecture.uncovered_feature_ids == ()


def test_a_feature_id_the_prd_never_minted_is_removed_and_reported():
    payload = dict(A_RESPONSE)
    payload["backend"] = [
        {"name": "Loyalty service", "feature_ids": ["F-9", "F-1"]},
    ]

    architecture = assemble_architecture(payload, a_prd())

    assert architecture.backend[0].feature_ids == ("F-1",)
    assert any("F-9" in note and "not in the PRD" in note for note in architecture.notes)


def test_a_feature_no_component_realises_is_reported_rather_than_hidden():
    payload = dict(A_RESPONSE)
    for key in ("backend", "web", "mobile", "data_flows", "integrations", "decisions"):
        payload[key] = []
    payload["backend"] = [{"name": "Payment service", "feature_ids": ["F-1"]}]

    architecture = assemble_architecture(payload, a_prd())

    assert architecture.uncovered_feature_ids == ("F-2",)
    assert any("F-2" in note for note in architecture.notes)
    # And the two client layers with nothing in them are called out.
    assert set(architecture.missing_layers) == {WEB, MOBILE}
    assert sum("no web application component" in note for note in architecture.notes) == 1


# --- The optional discussion ---------------------------------------------


def test_the_architecture_is_generated_without_a_discussion():
    architecture = generate_architecture(a_prd(), None, generate=responder(A_RESPONSE))

    assert architecture.components
    assert architecture.discussion_source == ""


def test_an_offered_discussion_is_quoted_and_recorded_as_evidence():
    prompts = []

    def record(prompt):
        prompts.append(prompt)
        return json.dumps(A_RESPONSE)

    architecture = generate_architecture(a_prd(), DISCUSSION, generate=record)

    assert "same API as the web client" in prompts[0]
    # The PRD still governs; the discussion is supporting evidence.
    assert "supporting evidence" in prompts[0]
    assert architecture.discussion_source == "microsoft_teams"


# --- Failure handling ----------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["", "not json at all", "42", '{"overview": "just prose"}', "null"],
    ids=["empty", "prose", "scalar", "no-components", "null"],
)
def test_a_response_that_is_not_an_architecture_is_refused(raw):
    with pytest.raises(ArchitectureResponseError):
        parse_architecture_response(raw)


def test_a_bare_list_is_read_as_components_and_scalars_in_it_are_dropped():
    """A list is the one shape worth rescuing; the rows inside it still have to be rows."""
    assert parse_architecture_response('[{"name": "Payment service"}]')["components"]

    architecture = generate_architecture(a_prd(), None, generate=responder("[1, 2, 3]"))

    assert architecture.is_baseline is True
    assert any("no usable component" in note for note in architecture.notes)


def test_a_malformed_response_falls_back_and_says_why_without_quoting_it():
    secret = "sk-live-should-never-be-shown"

    architecture = generate_architecture(a_prd(), None, generate=responder(secret))

    assert architecture.is_baseline is True
    assert architecture.components
    assert secret not in " ".join(architecture.notes)


def test_an_unreachable_provider_falls_back_to_the_deterministic_skeleton():
    def explode(prompt):
        raise RuntimeError("token=abcd1234 leaked in the message")

    architecture = generate_architecture(a_prd(), None, generate=explode)

    assert architecture.is_baseline is True
    assert "RuntimeError" in architecture.notes[0]
    assert "abcd1234" not in " ".join(architecture.notes)


def test_no_configured_provider_still_produces_a_traceable_skeleton():
    architecture = generate_architecture(a_prd(), None, generate=None)

    assert architecture.is_baseline is True
    assert architecture.missing_layers == ()
    for layer in LAYERS:
        assert [item.feature_ids for item in architecture.layer(layer)] == [("F-1",), ("F-2",)]
    assert architecture.decisions == () and architecture.flows == () and architecture.risks == ()
    assert "GEMINI_API_KEY" in architecture.notes[0]


def test_a_response_holding_no_usable_component_falls_back_and_keeps_both_notes():
    architecture = generate_architecture(
        a_prd(), None, generate=responder({"components": [{"responsibility": "no name"}]})
    )

    assert architecture.is_baseline is True
    assert any("no usable component" in note for note in architecture.notes)


def test_the_baseline_is_derived_from_the_prd_alone():
    baseline = build_baseline_architecture(a_prd())

    assert baseline.is_baseline is True
    assert len(baseline.components) == 6
    assert baseline.covered_feature_ids == ("F-1", "F-2")


def test_a_component_naming_no_layer_lands_in_the_backend_and_is_reported():
    architecture = assemble_architecture(
        {"components": [{"name": "Scheduler", "feature_ids": ["F-1"]}]}, a_prd()
    )

    assert architecture.components[0].layer == BACKEND
    assert any("no recognised layer" in note for note in architecture.notes)


# --- Lifecycle state -----------------------------------------------------


@pytest.mark.parametrize(
    "architecture, approved, expected",
    [
        (None, False, NOT_STARTED),
        (ArchitectureData(), False, NOT_STARTED),
        ("baseline", False, DRAFT),
        ("designed", False, PENDING_REVIEW),
        ("designed", True, APPROVED),
    ],
    ids=["none", "empty", "fallback", "designed", "approved"],
)
def test_the_stage_status_follows_the_architecture_that_actually_exists(
    architecture, approved, expected
):
    if architecture == "baseline":
        architecture = generate_architecture(a_prd(), None, generate=None)
    elif architecture == "designed":
        architecture = an_architecture()

    lifecycle = lifecycle_from(
        brd=a_brd(),
        brd_approved=True,
        prd=a_prd(),
        prd_approved=True,
        architecture=architecture,
        architecture_approved=approved,
    )

    state = lifecycle.state(ARCHITECTURE)
    assert state.status == expected
    assert state.detail
    assert lifecycle.architecture is architecture


def test_the_fallback_is_reported_as_a_draft_and_says_what_it_lacks():
    lifecycle = lifecycle_from(
        brd=a_brd(),
        brd_approved=True,
        prd=a_prd(),
        prd_approved=True,
        architecture=generate_architecture(a_prd(), None, generate=None),
    )

    detail = lifecycle.state(ARCHITECTURE).detail
    assert "deterministic fallback" in detail
    assert "no decisions, flows or risks" in detail


# --- The stage UI --------------------------------------------------------


def render_architecture_stage(monkeypatch, prd_approved=True, architecture=None, values=None,
                              clicked=(), response=A_RESPONSE, prd=None,
                              architecture_approved=False):
    """
    Render the architecture stage over the session state a real run would arrive with.

    Every widget is replaced with a recorder, so an untouched field returns its stored
    value and ``values`` names the one field a reviewer changed. ``clicked`` names the
    buttons pressed by key: nothing is generated or approved unless a test presses it.
    """
    prd = prd if prd is not None else a_prd()
    st.session_state[main.BRD_SESSION_KEY] = a_brd()
    st.session_state[main.BRD_APPROVED_SESSION_KEY] = True
    st.session_state[main.PRD_SESSION_KEY] = prd
    if prd_approved:
        st.session_state[main.PRD_APPROVED_SESSION_KEY] = True
    if architecture is not None:
        st.session_state[main.ARCHITECTURE_SESSION_KEY] = architecture
    if architecture_approved:
        st.session_state[main.ARCHITECTURE_APPROVED_SESSION_KEY] = True

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
        brd=a_brd(),
        brd_approved=True,
        prd=prd,
        prd_approved=prd_approved,
        architecture=st.session_state.get(main.ARCHITECTURE_SESSION_KEY),
        architecture_approved=bool(
            st.session_state.get(main.ARCHITECTURE_APPROVED_SESSION_KEY)
        ),
    )
    main._render_architecture_stage(lifecycle)
    return shown


def stored_architecture() -> ArchitectureData:
    return st.session_state[main.ARCHITECTURE_SESSION_KEY]


def test_pressing_generate_stores_an_architecture_and_approves_nothing(monkeypatch):
    render_architecture_stage(monkeypatch, clicked={"generate_architecture"})

    architecture = stored_architecture()
    assert len(architecture.components) == 5
    assert st.session_state.get(main.ARCHITECTURE_APPROVED_SESSION_KEY) is None


def test_nothing_is_generated_until_the_button_is_pressed(monkeypatch):
    shown = render_architecture_stage(monkeypatch, clicked=())

    assert main.ARCHITECTURE_SESSION_KEY not in st.session_state
    assert any("Not generated yet" in text for text in shown["caption"])


def test_an_unapproved_prd_offers_no_generate_control(monkeypatch):
    pressed = []
    monkeypatch.setattr(main.st, "button", lambda label, **k: pressed.append(k.get("key")))

    shown = render_architecture_stage(monkeypatch, prd_approved=False)

    assert pressed == []
    assert main.ARCHITECTURE_SESSION_KEY not in st.session_state
    assert any("approve it" in text for text in shown["info"])


def test_no_prd_in_the_session_says_so_instead_of_designing(monkeypatch):
    shown = render_architecture_stage(
        monkeypatch, prd=PRDData(), clicked={"generate_architecture"}
    )

    assert main.ARCHITECTURE_SESSION_KEY not in st.session_state
    assert any("No PRD" in text for text in shown["info"])


def test_a_pasted_discussion_reaches_the_generator(monkeypatch):
    render_architecture_stage(
        monkeypatch,
        clicked={"generate_architecture"},
        values={main._ARCH_WIDGET_PREFIX + "discussion_text": DISCUSSION.raw_text},
    )

    assert stored_architecture().discussion_source == "manual"


def test_an_edit_is_kept_and_leaves_the_architecture_pending_review(monkeypatch):
    architecture = an_architecture()
    first = architecture.components[0]

    render_architecture_stage(
        monkeypatch,
        architecture=architecture,
        values={
            main._ARCH_WIDGET_PREFIX + "overview": "One API, two clients, one gateway.",
            "{}{}_name".format(main._ARCH_WIDGET_PREFIX, first.component_id): "Payments API",
        },
    )

    edited = stored_architecture()
    assert edited.overview == "One API, two clients, one gateway."
    assert edited.components[0].name == "Payments API"
    # Editing does not approve, and does not rewrite traceability.
    assert st.session_state.get(main.ARCHITECTURE_APPROVED_SESSION_KEY) is None
    assert edited.components[0].feature_ids == first.feature_ids


def test_approval_is_explicit_and_only_the_button_grants_it(monkeypatch):
    render_architecture_stage(monkeypatch, architecture=an_architecture())
    assert st.session_state.get(main.ARCHITECTURE_APPROVED_SESSION_KEY) is None

    render_architecture_stage(
        monkeypatch, architecture=an_architecture(), clicked={"approve_architecture"}
    )
    assert st.session_state[main.ARCHITECTURE_APPROVED_SESSION_KEY] is True


def test_an_approved_architecture_is_read_only_until_approval_is_revoked(monkeypatch):
    architecture = an_architecture()
    shown = render_architecture_stage(
        monkeypatch,
        architecture=architecture,
        architecture_approved=True,
        values={main._ARCH_WIDGET_PREFIX + "overview": "rewritten while approved"},
    )

    assert any("approved" in text for text in shown["success"])
    assert stored_architecture().overview == architecture.overview

    render_architecture_stage(
        monkeypatch,
        architecture=architecture,
        architecture_approved=True,
        clicked={"revoke_architecture_approval"},
    )
    assert st.session_state[main.ARCHITECTURE_APPROVED_SESSION_KEY] is False


def test_a_prd_changed_after_generation_warns_that_the_design_is_stale(monkeypatch):
    stale = replace(an_architecture(), source_feature_ids=("F-1",))

    shown = render_architecture_stage(monkeypatch, architecture=stale)

    assert any("Regenerate" in text for text in shown["warning"])


def test_regenerating_the_prd_drops_the_architecture_and_its_approval():
    st.session_state[main.ARCHITECTURE_SESSION_KEY] = an_architecture()
    st.session_state[main.ARCHITECTURE_APPROVED_SESSION_KEY] = True
    st.session_state[main._ARCH_WIDGET_PREFIX + "overview"] = "edited"

    main._clear_prd_state()

    for key in (
        main.ARCHITECTURE_SESSION_KEY,
        main.ARCHITECTURE_APPROVED_SESSION_KEY,
        main._ARCH_WIDGET_PREFIX + "overview",
    ):
        assert key not in st.session_state
