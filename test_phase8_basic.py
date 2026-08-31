#!/usr/bin/env python3
"""Focused tests for Phase 8 Test Execution / Delivery Tracking."""

import sys
sys.path.insert(0, '.')

from test_case_models import (
    TestCase, TestSuite,
    TEST_EXECUTION_NOT_RUN, TEST_EXECUTION_PASS, TEST_EXECUTION_FAIL, TEST_EXECUTION_BLOCKED
)
from lifecycle_models import (
    _test_execution_state,
    NOT_STARTED, IN_PROGRESS, COMPLETED, PENDING_REVIEW,
    TEST_EXECUTION, IMPLEMENTED_STAGES, LIFECYCLE_STAGES, STAGE_LABEL
)

# --- All execution states ---

def test_execution_state_not_run():
    """Test that Not Run status is handled correctly."""
    test_case = TestCase(
        test_id="TC-001",
        story_reference="STORY-001",
        scenario="Test 1",
        execution_status=TEST_EXECUTION_NOT_RUN
    )
    suite = TestSuite(story_id="STORY-001", test_cases=[test_case])
    status, detail = _test_execution_state([suite])
    assert status == PENDING_REVIEW
    assert "0 passed" in detail
    assert "0 failed" in detail
    assert "0 blocked" in detail
    assert "1 not run" in detail
    print("✓ Not Run status handled correctly")

def test_execution_state_pass():
    """Test that Pass status is handled correctly."""
    test_case = TestCase(
        test_id="TC-001",
        story_reference="STORY-001",
        scenario="Test 1",
        execution_status=TEST_EXECUTION_PASS,
        actual_result="All checks passed",
        notes="No issues"
    )
    suite = TestSuite(story_id="STORY-001", test_cases=[test_case])
    status, detail = _test_execution_state([suite])
    assert status == COMPLETED
    assert "1 passed" in detail
    assert "0 failed" in detail
    assert "0 blocked" in detail
    assert "0 not run" in detail
    print("✓ Pass status handled correctly")

def test_execution_state_fail():
    """Test that Fail status is handled correctly."""
    test_case = TestCase(
        test_id="TC-001",
        story_reference="STORY-001",
        scenario="Test 1",
        execution_status=TEST_EXECUTION_FAIL,
        actual_result="Expected value 5 but got 3",
        notes="Need to fix validation logic",
        defect_reference="BUG-123"
    )
    suite = TestSuite(story_id="STORY-001", test_cases=[test_case])
    status, detail = _test_execution_state([suite])
    assert status == IN_PROGRESS
    assert "0 passed" in detail
    assert "1 failed" in detail
    assert "0 blocked" in detail
    assert "0 not run" in detail
    print("✓ Fail status handled correctly")

def test_execution_state_blocked():
    """Test that Blocked status is handled correctly."""
    test_case = TestCase(
        test_id="TC-001",
        story_reference="STORY-001",
        scenario="Test 1",
        execution_status=TEST_EXECUTION_BLOCKED,
        notes="Waiting for environment setup"
    )
    suite = TestSuite(story_id="STORY-001", test_cases=[test_case])
    status, detail = _test_execution_state([suite])
    assert status == IN_PROGRESS
    assert "0 passed" in detail
    assert "0 failed" in detail
    assert "1 blocked" in detail
    assert "0 not run" in detail
    print("✓ Blocked status handled correctly")

def test_execution_state_mixed():
    """Test mixed execution statuses."""
    test_cases = [
        TestCase(test_id="TC-001", story_reference="STORY-001", scenario="Test 1", execution_status=TEST_EXECUTION_PASS),
        TestCase(test_id="TC-002", story_reference="STORY-001", scenario="Test 2", execution_status=TEST_EXECUTION_FAIL),
        TestCase(test_id="TC-003", story_reference="STORY-001", scenario="Test 3", execution_status=TEST_EXECUTION_BLOCKED),
        TestCase(test_id="TC-004", story_reference="STORY-001", scenario="Test 4", execution_status=TEST_EXECUTION_NOT_RUN),
    ]
    suite = TestSuite(story_id="STORY-001", test_cases=test_cases)
    status, detail = _test_execution_state([suite])
    assert status == IN_PROGRESS  # Has failures
    assert "1 passed" in detail
    assert "1 failed" in detail
    assert "1 blocked" in detail
    assert "1 not run" in detail
    print("✓ Mixed execution statuses handled correctly")

# --- Result/notes/defect fields ---

def test_execution_result_fields():
    """Test that result, notes, and defect fields are preserved."""
    test_case = TestCase(
        test_id="TC-001",
        story_reference="STORY-001",
        scenario="Test login",
        execution_status=TEST_EXECUTION_FAIL,
        actual_result="Login failed with error 401",
        notes="Authentication service returned 401",
        defect_reference="BUG-456"
    )
    assert test_case.actual_result == "Login failed with error 401"
    assert test_case.notes == "Authentication service returned 401"
    assert test_case.defect_reference == "BUG-456"
    print("✓ Result, notes, and defect fields preserved")

def test_execution_result_defaults():
    """Test that result, notes, and defect fields have proper defaults."""
    test_case = TestCase(
        test_id="TC-001",
        story_reference="STORY-001",
        scenario="Test login"
    )
    assert test_case.actual_result == ""
    assert test_case.notes == ""
    assert test_case.defect_reference == ""
    assert test_case.execution_status == TEST_EXECUTION_NOT_RUN
    print("✓ Result, notes, and defect fields have proper defaults")

# --- Invalid/missing test case handling ---

def test_execution_state_none():
    """Test that None input is handled correctly."""
    status, detail = _test_execution_state(None)
    assert status == NOT_STARTED
    assert "No test cases have been generated yet" in detail
    print("✓ None input handled correctly")

def test_execution_state_empty_list():
    """Test that empty list input is handled correctly."""
    status, detail = _test_execution_state([])
    assert status == NOT_STARTED
    assert "No test cases available" in detail
    print("✓ Empty list input handled correctly")

def test_execution_state_empty_suite():
    """Test that suite with no test cases is handled correctly."""
    suite = TestSuite(story_id="STORY-001", test_cases=[])
    status, detail = _test_execution_state([suite])
    assert status == NOT_STARTED
    print("✓ Empty suite handled correctly")

# --- Traceability ---

def test_execution_traceability_fields():
    """Test that traceability fields are preserved."""
    test_case = TestCase(
        test_id="TC-001",
        story_reference="STORY-001",
        scenario="Test login",
        execution_status=TEST_EXECUTION_PASS
    )
    assert test_case.test_id == "TC-001"
    assert test_case.story_reference == "STORY-001"
    print("✓ Traceability fields preserved")

def test_execution_suite_traceability():
    """Test that suite traceability is preserved."""
    test_case = TestCase(
        test_id="TC-001",
        story_reference="STORY-001",
        scenario="Test login"
    )
    suite = TestSuite(story_id="STORY-001", test_cases=[test_case])
    assert suite.story_id == "STORY-001"
    assert len(suite.test_cases) == 1
    assert suite.test_cases[0].test_id == "TC-001"
    print("✓ Suite traceability preserved")

# --- Lifecycle integration ---

def test_lifecycle_includes_test_execution():
    """Test that TEST_EXECUTION is in the lifecycle."""
    assert TEST_EXECUTION in LIFECYCLE_STAGES
    assert STAGE_LABEL[TEST_EXECUTION] == "Test Execution"
    print("✓ TEST_EXECUTION in lifecycle stages")

def test_lifecycle_implemented_stages():
    """Test that TEST_EXECUTION is in implemented stages."""
    assert TEST_EXECUTION in IMPLEMENTED_STAGES
    print("✓ TEST_EXECUTION in implemented stages")

# --- Completion/Jira protection ---

def test_execution_status_states():
    """Test that execution status constants are defined correctly."""
    assert TEST_EXECUTION_NOT_RUN == "Not Run"
    assert TEST_EXECUTION_PASS == "Pass"
    assert TEST_EXECUTION_FAIL == "Fail"
    assert TEST_EXECUTION_BLOCKED == "Blocked"
    print("✓ Execution status constants defined correctly")

def test_execution_state_no_jira_mutation():
    """Test that _test_execution_state doesn't mutate test cases."""
    test_case = TestCase(
        test_id="TC-001",
        story_reference="STORY-001",
        scenario="Test",
        execution_status=TEST_EXECUTION_PASS
    )
    suite = TestSuite(story_id="STORY-001", test_cases=[test_case])
    original_status = test_case.execution_status
    original_scenario = test_case.scenario

    _test_execution_state([suite])

    assert test_case.execution_status == original_status
    assert test_case.scenario == original_scenario
    print("✓ No mutation of test cases")

# --- Regressions to Phases 1-7 ---

def test_regression_test_cases_model():
    """Test that Phase 7 test case fields still work."""
    test_case = TestCase(
        test_id="TC-001",
        story_reference="STORY-001",
        scenario="Test login",
        preconditions="User exists",
        steps="1. Login\n2. Verify",
        expected_result="Success",
        priority="High",
        test_type="Functional",
        is_approved=True
    )
    assert test_case.test_id == "TC-001"
    assert test_case.is_approved == True
    assert test_case.priority == "High"
    assert test_case.test_type == "Functional"
    print("✓ Phase 7 test case fields still work")

def test_regression_test_suite_properties():
    """Test that Phase 7 test suite properties still work."""
    test_cases = [
        TestCase(test_id="TC-001", story_reference="STORY-001", scenario="Test 1", test_type="Functional", priority="High"),
        TestCase(test_id="TC-002", story_reference="STORY-001", scenario="Test 2", test_type="Negative", priority="Medium"),
    ]
    suite = TestSuite(story_id="STORY-001", test_cases=test_cases)
    assert len(suite.functional_tests) == 1
    assert len(suite.negative_tests) == 1
    assert len(suite.high_priority_tests) == 1
    assert len(suite.medium_priority_tests) == 1
    print("✓ Phase 7 test suite properties still work")


if __name__ == "__main__":
    print("Running Phase 8 focused tests...\n")

    # All execution states
    test_execution_state_not_run()
    test_execution_state_pass()
    test_execution_state_fail()
    test_execution_state_blocked()
    test_execution_state_mixed()

    # Result/notes/defect fields
    test_execution_result_fields()
    test_execution_result_defaults()

    # Invalid/missing test case handling
    test_execution_state_none()
    test_execution_state_empty_list()
    test_execution_state_empty_suite()

    # Traceability
    test_execution_traceability_fields()
    test_execution_suite_traceability()

    # Lifecycle integration
    test_lifecycle_includes_test_execution()
    test_lifecycle_implemented_stages()

    # Completion/Jira protection
    test_execution_status_states()
    test_execution_state_no_jira_mutation()

    # Regressions to Phases 1-7
    test_regression_test_cases_model()
    test_regression_test_suite_properties()

    print("\n✓ All Phase 8 focused tests passed!")