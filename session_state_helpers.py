"""
Pure session state helpers for the Auto-BRD application lifecycle.

This module centralizes all pure session state handling (no UI rendering)
to reduce complexity in main.py. These functions only read/write session state
and do not call any Streamlit UI functions.
"""

from typing import Optional, List, Any

from brd_models import BRDData
from prd_models import PRDData
from architecture_models import ArchitectureData
from implementation_plan_models import ImplementationPlan
from test_case_models import TestSuite
from lifecycle_models import LIFECYCLE_STAGES, DISCOVERY_BRD, PRD, ARCHITECTURE, IMPLEMENTATION_PLAN, TEST_CASES, TEST_EXECUTION, DELIVERY_STATUS, SPRINT_PLAN
from sprint_models import SprintPlan
from jira_models import CreatedIssue
from delivery_mapping_builder import DeliveryMapping

# Session state keys for lifecycle data
BRD_SESSION_KEY = "brd_data"
BRD_SOURCE_SESSION_KEY = "brd_source"
BRD_APPROVED_SESSION_KEY = "brd_approved"

PRD_SESSION_KEY = "prd_data"
PRD_APPROVED_SESSION_KEY = "prd_approved"
PRD_REFINEMENT_SESSION_KEY = "prd_refinement"
_PRD_WIDGET_PREFIX = "prd_review__"

ARCHITECTURE_SESSION_KEY = "architecture_data"
ARCHITECTURE_APPROVED_SESSION_KEY = "architecture_approved"
ARCHITECTURE_DISCUSSION_SESSION_KEY = "architecture_discussion"
_ARCH_WIDGET_PREFIX = "arch_review__"

IMPLEMENTATION_PLAN_SESSION_KEY = "implementation_plan_data"
IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY = "implementation_plan_approved"
_PLAN_WIDGET_PREFIX = "plan_review__"

TEST_CASES_SESSION_KEY = "test_cases_data"
TEST_CASES_APPROVED_SESSION_KEY = "test_cases_approved"
_TEST_CASES_WIDGET_PREFIX = "test_cases_review__"

TEST_EXECUTION_SESSION_KEY = "test_execution_data"
TEST_EXECUTION_APPROVED_SESSION_KEY = "test_execution_approved"
_TEST_EXECUTION_WIDGET_PREFIX = "test_execution_review__"

SPRINT_COMPLETION_HISTORY_KEY = "sprint_completion_history"
SPRINT_COMPLETION_LAST_KEY = "sprint_completion_last"
SPRINT_COMPLETION_NEXT_KEY = "sprint_completion_next"
SPRINT_COMPLETION_APPROVED_KEY = "sprint_completion_next_approved"
# NOTE: SPRINT_PLAN_SESSION_KEY and SPRINT_PLAN_APPROVED_SESSION_KEY are defined in lifecycle_models.py

SPRINT_PLAN_SESSION_KEY = "sprint_plan_data"
SPRINT_PLAN_APPROVED_SESSION_KEY = "sprint_plan_approved"
SPRINT_PLAN_GENERATED_KEY = "sprint_plan_generated"
_SPRINT_WIDGET_PREFIX = "sprint_plan_review__"

# Jira session state suffixes
_JIRA_PROJECT_SUFFIXES = (
    "projects",
    "projects_site",
    "project",
    "metadata",
    "metadata_for",
    "plan",
    "plan_for",
    # JIRA-007. "created" holds the per-issue outcome of a creation run and
    # "creating" is the in-flight guard that stops a rerun from writing twice. Both
    # belong to one plan against one target, so every path that drops a plan drops
    # them too -- otherwise a fresh plan would inherit another plan's results.
    "created",
    "creating",
    "confirm_create",
    # The stable plan item -> issue key mapping an approved implementation plan
    # produced in this project, the last run's per-item outcomes, and the guards for
    # that one write. A mapping is only meaningful against the project its keys live
    # in, so choosing a different project or disconnecting drops it with everything
    # else here. The mapped proposal itself is not stored: it is recomputed from the
    # approved plan and this project's metadata, so the preview cannot drift from the
    # plan the reviewer approved.
    "delivery_mapping",
    "delivery_results",
    "delivery_creating",
    "delivery_confirm",
)

# Streamlit widget keys for the work-plan review editors. Not under ``jira__``:
# those suffixes are plan data, and a leftover text-input value would otherwise
# outlive the plan it described.
_JIRA_REVIEW_WIDGET_PREFIX = "jira_review__"

# Session-state namespace for Jira. Spelled once so the work-plan panel can address
# its own keys without being handed the service object: with no service and no token
# in scope, that panel has no way to reach Jira at all.
JIRA_STATE_NAME = "jira"  # JiraService().name would create instance, so hardcode


# Session state key for uploaded transcript
UPLOADED_TRANSCRIPT_KEY = "uploaded_transcript"


def _skey(provider_name: str, suffix: str) -> str:
    """Namespaced session-state key for one provider."""
    return f"{provider_name}__{suffix}"


def _clear_prd_widgets() -> None:
    """Drop PRD review-editor widget state."""
    import streamlit as st
    for key in list(st.session_state.keys()):
        if str(key).startswith(_PRD_WIDGET_PREFIX):
            st.session_state.pop(key, None)


def _clear_sprint_plan_state() -> None:
    """
    Forget the generated sprint plan, its approval and its editors.

    Called when the implementation plan changes for the same reason downstream stages
    are cleared when their upstream artifact changes: a sprint plan is a planning of one
    specific implementation plan, so a new plan makes a held sprint plan wrong rather
    than merely old, and its approval cannot carry over to work nobody
    has reviewed.
    """
    import streamlit as st
    for key in (
        SPRINT_PLAN_SESSION_KEY,
        SPRINT_PLAN_APPROVED_SESSION_KEY,
        SPRINT_PLAN_GENERATED_KEY,
    ):
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if str(key).startswith(_SPRINT_WIDGET_PREFIX):
            st.session_state.pop(key, None)


def _held_sprint_plan() -> Optional[SprintPlan]:
    """Get the held sprint plan from session state, or None if not present or wrong type."""
    import streamlit as st
    plan = st.session_state.get(SPRINT_PLAN_SESSION_KEY)
    return plan if isinstance(plan, SprintPlan) else None


def _persist_sprint_plan(plan: SprintPlan) -> SprintPlan:
    """Store a sprint plan in session state."""
    import streamlit as st
    st.session_state[SPRINT_PLAN_SESSION_KEY] = plan
    return plan


def _clear_test_cases_state() -> None:
    """Forget the test cases, their approval, and their review editors."""
    import streamlit as st
    for key in (
        TEST_CASES_SESSION_KEY,
        TEST_CASES_APPROVED_SESSION_KEY,
    ):
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if str(key).startswith(_TEST_CASES_WIDGET_PREFIX):
            st.session_state.pop(key, None)


def _clear_test_execution_state() -> None:
    """Forget the test execution data, their approval, and their review editors."""
    import streamlit as st
    for key in (
        TEST_EXECUTION_SESSION_KEY,
        TEST_EXECUTION_APPROVED_SESSION_KEY,
    ):
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if str(key).startswith(_TEST_EXECUTION_WIDGET_PREFIX):
            st.session_state.pop(key, None)


def _held_test_execution() -> Optional[List[Any]]:
    """Get the held test execution data from session state, or None if not present or wrong type."""
    import streamlit as st
    test_execution = st.session_state.get(TEST_EXECUTION_SESSION_KEY)
    return test_execution if isinstance(test_execution, (list, tuple)) else None


def _persist_test_execution(test_execution: List[Any]) -> List[Any]:
    """Store test execution data in session state."""
    import streamlit as st
    st.session_state[TEST_EXECUTION_SESSION_KEY] = test_execution
    return test_execution


def _clear_implementation_plan_state() -> None:
    """
    Forget the implementation plan, its approval and its editors.

    Called when the architecture changes, for the reason the architecture is cleared when
    the PRD changes: a plan decomposes one specific design, so a new design makes a held
    plan wrong rather than merely old, and its approval cannot carry over to work nobody
    has reviewed.
    """
    import streamlit as st
    for key in (
        IMPLEMENTATION_PLAN_SESSION_KEY,
        IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY,
    ):
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if str(key).startswith(_PLAN_WIDGET_PREFIX):
            st.session_state.pop(key, None)
    _clear_test_cases_state()
    _clear_sprint_plan_state()


def _clear_architecture_state() -> None:
    """
    Forget the architecture, its approval and its editors.

    Called when the PRD changes for the same reason ``_clear_prd_state`` is called when
    the PRD is a product definition of one specific BRD, so a new BRD makes a held PRD wrong
    rather than merely old, and its approval cannot carry over to a document nobody has
    reviewed. The architecture derived from that PRD goes with it.
    """
    import streamlit as st
    for key in (
        ARCHITECTURE_SESSION_KEY,
        ARCHITECTURE_APPROVED_SESSION_KEY,
        ARCHITECTURE_DISCUSSION_SESSION_KEY,
    ):
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if str(key).startswith(_ARCH_WIDGET_PREFIX):
            st.session_state.pop(key, None)
    _clear_implementation_plan_state()
    _clear_test_cases_state()


def _clear_prd_state() -> None:
    """
    Forget the PRD, its approval and its editors.

    Called when the BRD changes: a PRD is a product definition of one specific BRD, so
    a new BRD makes a held PRD wrong rather than merely old, and its approval cannot
    carry over to a document nobody has reviewed. The architecture derived from that PRD
    goes with it.
    """
    import streamlit as st
    for key in (PRD_SESSION_KEY, PRD_APPROVED_SESSION_KEY, PRD_REFINEMENT_SESSION_KEY):
        st.session_state.pop(key, None)
    _clear_prd_widgets()
    _clear_architecture_state()


def _clear_jira_plan_review_widgets() -> None:
    """Drop review-editor widget state so a new or absent plan cannot inherit it."""
    import streamlit as st
    for key in list(st.session_state.keys()):
        if str(key).startswith(_JIRA_REVIEW_WIDGET_PREFIX):
            st.session_state.pop(key, None)


def _disconnect(provider_name: str) -> None:
    """Drop every trace of the provider session from this browser session."""
    import streamlit as st
    suffixes = ("tokens", "handshake", "discovery", "transcript", "identity", "sites", "site")
    for suffix in suffixes + _JIRA_PROJECT_SUFFIXES:
        st.session_state.pop(_skey(provider_name, suffix), None)
    _clear_jira_plan_review_widgets()


def _clear_jira_project_state(provider_name: str) -> None:
    """
    Forget the project list, the project selection and its metadata.

    The picker's own widget state goes too: a shorter new list would leave a
    stored index pointing past the end of it.
    """
    import streamlit as st
    for suffix in _JIRA_PROJECT_SUFFIXES:
        st.session_state.pop(_skey(provider_name, suffix), None)
    st.session_state.pop("select_jira_project", None)
    _clear_jira_plan_review_widgets()


def _store_brd(brd_data: BRDData, source: str = "") -> None:
    """
    Keep the generated BRD for the optional Jira step.

    Required because every button click re-runs this script: by the time a
    "Generate Jira Work Plan" click is handled, the run that produced the BRD is
    over and its local variable is gone. Nothing else about BRD generation,
    validation, display or export changes.

    A plan built from the previous BRD is dropped rather than left behind. A work
    plan is a proposal about one specific BRD, so a newly generated BRD makes a
    cached plan wrong, not merely old. The same applies to a PRD and to any approval
    recorded against the BRD that has just been replaced.
    """
    import streamlit as st
    st.session_state[BRD_SESSION_KEY] = brd_data
    st.session_state[BRD_SOURCE_SESSION_KEY] = str(source or "")
    st.session_state.pop(BRD_APPROVED_SESSION_KEY, None)
    _clear_prd_state()
    for suffix in (
        "plan",
        "plan_for",
        "created",
        "creating",
        "confirm_create",
    ):
        st.session_state.pop(_skey(JIRA_STATE_NAME, suffix), None)
    _clear_jira_plan_review_widgets()


def _held_brd() -> Optional[BRDData]:
    """Get the held BRD from session state, or None if not present or wrong type."""
    import streamlit as st
    brd = st.session_state.get(BRD_SESSION_KEY)
    return brd if isinstance(brd, BRDData) else None


def _held_prd() -> Optional[PRDData]:
    """Get the held PRD from session state, or None if not present or wrong type."""
    import streamlit as st
    prd = st.session_state.get(PRD_SESSION_KEY)
    return prd if isinstance(prd, PRDData) else None


def _persist_prd(prd: PRDData) -> PRDData:
    """Store a PRD in session state."""
    import streamlit as st
    st.session_state[PRD_SESSION_KEY] = prd
    return prd


def _held_architecture() -> Optional[ArchitectureData]:
    """Get the held architecture from session state, or None if not present or wrong type."""
    import streamlit as st
    architecture = st.session_state.get(ARCHITECTURE_SESSION_KEY)
    return architecture if isinstance(architecture, ArchitectureData) else None


def _persist_architecture(architecture: ArchitectureData) -> ArchitectureData:
    """Store an architecture in session state."""
    import streamlit as st
    st.session_state[ARCHITECTURE_SESSION_KEY] = architecture
    return architecture


def _held_implementation_plan() -> Optional[ImplementationPlan]:
    """Get the held implementation plan from session state, or None if not present or wrong type."""
    import streamlit as st
    plan = st.session_state.get(IMPLEMENTATION_PLAN_SESSION_KEY)
    return plan if isinstance(plan, ImplementationPlan) else None


def _persist_implementation_plan(plan: ImplementationPlan) -> ImplementationPlan:
    """Store an implementation plan in session state."""
    import streamlit as st
    st.session_state[IMPLEMENTATION_PLAN_SESSION_KEY] = plan
    _clear_test_cases_state()
    return plan


def _held_test_cases() -> Optional[List[TestSuite]]:
    """Get the held test cases from session state, or None if not present or wrong type."""
    import streamlit as st
    test_cases = st.session_state.get(TEST_CASES_SESSION_KEY)
    return test_cases if isinstance(test_cases, (list, tuple)) else None


def _persist_test_cases(test_cases: List[TestSuite]) -> List[TestSuite]:
    """Store test cases in session state."""
    import streamlit as st
    st.session_state[TEST_CASES_SESSION_KEY] = test_cases
    return test_cases


def _brd_approved() -> bool:
    """Whether the reviewer approved the BRD held in this session."""
    import streamlit as st
    return bool(st.session_state.get(BRD_APPROVED_SESSION_KEY))


def _prd_approved() -> bool:
    """Whether the reviewer approved the PRD held in this session."""
    import streamlit as st
    return bool(st.session_state.get(PRD_APPROVED_SESSION_KEY))


def _architecture_approved() -> bool:
    """Whether the reviewer approved the architecture held in this session."""
    import streamlit as st
    return bool(st.session_state.get(ARCHITECTURE_APPROVED_SESSION_KEY))


def _implementation_plan_approved() -> bool:
    """Whether the reviewer approved the implementation plan held in this session."""
    import streamlit as st
    return bool(st.session_state.get(IMPLEMENTATION_PLAN_APPROVED_SESSION_KEY))


def _test_cases_approved() -> bool:
    """Whether the reviewer approved the test cases held in this session."""
    import streamlit as st
    return bool(st.session_state.get(TEST_CASES_APPROVED_SESSION_KEY))


def _test_execution_approved() -> bool:
    """Whether the reviewer approved the test execution held in this session."""
    import streamlit as st
    return bool(st.session_state.get(TEST_EXECUTION_APPROVED_SESSION_KEY))


def _sprint_plan_approved() -> bool:
    """Whether the reviewer approved the sprint plan held in this session."""
    import streamlit as st
    return bool(st.session_state.get(SPRINT_PLAN_APPROVED_SESSION_KEY))


def store_uploaded_transcript(uploaded_file) -> Any:
    """
    Store an uploaded transcript file in session state.

    Takes an uploaded file object (as from st.file_uploader),
    normalizes it using the real normalize_uploaded_file function,
    stores the resulting NormalizedTranscript in session state
    under UPLOADED_TRANSCRIPT_KEY, and returns the transcript.

    This is the real application boundary: UploadedFile → normalize → session state.
    """
    import streamlit as st
    from transcript_processor import normalize_uploaded_file

    # Normalize the uploaded file using the real function
    transcript = normalize_uploaded_file(uploaded_file)

    # Store the result in the real session state
    st.session_state[UPLOADED_TRANSCRIPT_KEY] = transcript

    # Return the transcript for immediate use if needed
    return transcript