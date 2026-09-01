"""
Test Execution & Delivery Evidence

Executes repository tests for approved test cases, captures execution evidence,
and provides a human approval step before this evidence is consumed by Sprint Completion.

The execution layer is separate from test case generation:
- test_case_generator.py only creates test case specifications
- test_execution.py executes those tests against the actual codebase
- sprint_completion.py consumes execution evidence for story completion decisions

Execution mapping:
- exit code 0 → Pass
- non-zero test failure → Fail
- unable/unexecutable → Blocked/Not Run with clear reason
- no executable mapping → Not Run

Safety:
- workspace confinement, timeout, safe subprocess
- no arbitrary path traversal
- evidence captured with test IDs and story references
- human approval required before delivery evidence
"""

import os
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any

from test_case_models import TestCase, TestSuite, TEST_EXECUTION_NOT_RUN, TEST_EXECUTION_PASS, TEST_EXECUTION_FAIL, TEST_EXECUTION_BLOCKED

logger = logging.getLogger(__name__)

# Maximum execution time in seconds
MAX_EXECUTION_TIME = 300

# File patterns that indicate executable tests
TEST_PATTERNS = [
    "test_*.py",
    "*_test.py",
    "*/tests/*.py",
    "*/test/*.py"
]

# Stories that require test evidence (most stories do)
STORY_REQUIRES_TESTING = True

@dataclass
class ExecutionResult:
    """Result of executing a single test case."""
    test_id: str
    story_reference: str
    execution_status: str = TEST_EXECUTION_NOT_RUN
    actual_result: str = ""
    notes: str = ""
    defect_reference: str = ""
    command_executed: str = ""
    exit_code: int = -1
    timestamp: float = 0.0

@dataclass
class TestExecutionSession:
    """Execution session for a story: tracks files changed and execution results."""
    story_id: str
    workspace_root: str
    execution_results: List[ExecutionResult] = field(default_factory=list)
    files_changed: List[str] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    overall_status: str = "Not Started"
    human_approval_required: bool = True
    human_approval: bool = False
    human_approval_notes: str = ""

@dataclass
class TestExecutionEvidence:
    """Complete evidence package for test execution."""
    story_id: str
    session: TestExecutionSession
    summary: str = ""
    approved: bool = False
    delivery_ready: bool = False


def _resolve_test_files(workspace_root: str) -> List[str]:
    """Find Python test files inside the workspace.

    Safety: confines to workspace_root, respects typical test naming patterns.
    """
    test_files: List[str] = []
    workspace_abs = os.path.abspath(workspace_root)

    for root, dirs, files in os.walk(workspace_abs):
        # Skip cache and sensitive directories
        if "__pycache__" in root or ".pytest_cache" in root:
            continue
        if ".git" in root:
            continue
        if "node_modules" in root:
            continue

        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]

        for name in files:
            # Check if file matches test patterns
            if name.endswith('.py') and _is_test_file(name):
                full_path = os.path.join(root, name)
                test_files.append(full_path)

    return sorted(test_files)


def _is_test_file(filename: str) -> bool:
    """Check if a file matches test naming patterns."""
    basename = os.path.basename(filename)

    # Explicit test file patterns
    if basename.startswith('test_') and basename.endswith('.py'):
        return True
    if basename.endswith('_test.py'):
        return True

    # Directory patterns
    if '/tests/' in filename or '/test/' in filename:
        return True

    return False


def _safe_execute_command(command: List[str], workspace_root: str, timeout: int = MAX_EXECUTION_TIME) -> Tuple[int, str, str]:
    """Safely execute a command in the workspace.

    Safety: confined to workspace, timeout, captures output, safe error handling.
    """
    try:
        # Validate workspace root exists and is absolute
        workspace_abs = os.path.abspath(workspace_root)
        if not os.path.exists(workspace_abs):
            return -1, f"Workspace does not exist: {workspace_root}", "Workspace not found"

        result = subprocess.run(
            command,
            cwd=workspace_abs,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, 'PYTHONPATH': workspace_abs}
        )

        return result.returncode, result.stdout, result.stderr

    except subprocess.TimeoutExpired:
        return -1, f"Command timed out after {timeout} seconds:", f"Timeout: {' '.join(command)}"
    except FileNotFoundError:
        return -2, f"Command not found: {' '.join(command)}", f"Command failed: {' '.join(command)}"
    except Exception as e:
        return -3, f"Execution error: {str(e)}", f"Unexpected error: {type(e).__name__}"


def _map_test_case_to_pytest(test_case: TestCase, story_id: str) -> Optional[str]:
    """Map a test case specification to a pytest command or file.

    This is a minimal mapping that follows the existing coding_agent.py patterns.
    For now, we execute all tests in the repository and map results by story.
    """
    # For simplicity and safety, we run all tests in the repository
    # Each test case is checked against its story reference
    return "pytest"


def _find_applicable_tests(test_cases: List[TestCase], story_id: str) -> List[TestCase]:
    """Find test cases that apply to a specific story."""
    applicable = [tc for tc in test_cases if tc.story_reference == story_id]
    return applicable


def _execute_single_test_case(test_case: TestCase, workspace_root: str, story_id: str) -> ExecutionResult:
    """Execute a single test case and return result.

    For now, we run the entire test suite and map back to individual test cases
    based on story reference. This is a safe approach that doesn't require
    fine-grained test identification.
    """
    timestamp = time.time()

    # Build command to run all tests (existing pattern from coding_agent.py)
    test_files = _resolve_test_files(workspace_root)
    if not test_files:
        return ExecutionResult(
            test_id=test_case.test_id,
            story_reference=story_id,
            execution_status=TEST_EXECUTION_BLOCKED,
            notes="No test files found in repository",
            timestamp=timestamp
        )

    command = [sys.executable, "-m", "pytest", *test_files, "--tb=short", "-p", "no:cacheprovider"]

    # For now, we'll run the full test suite once per story execution
    # In a more sophisticated implementation, we could map individual test cases
    # to specific pytest tests or functions
    exit_code, stdout, stderr = _safe_execute_command(command, workspace_root)

    # Determine execution status based on exit code
    if exit_code == 0:
        execution_status = TEST_EXECUTION_PASS
        actual_result = "All tests passed"
    else:
        # Distinguish between test failures and execution problems
        if "No tests ran" in stderr or "collected 0 items" in stdout:
            execution_status = TEST_EXECUTION_BLOCKED
            actual_result = "No tests found to run"
        elif exit_code == -1:
            execution_status = TEST_EXECUTION_BLOCKED
            actual_result = "Test execution timed out"
        elif exit_code == -2:
            execution_status = TEST_EXECUTION_BLOCKED
            actual_result = "Test command not found"
        elif exit_code == -3:
            execution_status = TEST_EXECUTION_BLOCKED
            actual_result = "Test execution error"
        else:
            execution_status = TEST_EXECUTION_FAIL
            # Capture failure details for reporting
            stderr_excerpt = stderr[:500] if stderr else ""
            actual_result = f"Tests failed (exit {exit_code}). {stderr_excerpt}"

    return ExecutionResult(
        test_id=test_case.test_id,
        story_reference=story_id,
        execution_status=execution_status,
        actual_result=actual_result,
        command_executed=" ".join(command),
        exit_code=exit_code,
        timestamp=timestamp,
        notes=f"Test file resolution: {len(test_files)} test files found"
    )


def execute_story_tests(story_id: str, test_cases: List[TestCase], workspace_root: str) -> TestExecutionSession:
    """Execute all test cases for a story.

    This is the main entry point for test execution. It creates a session,
    runs all applicable tests, and returns the session with results.
    """
    session = TestExecutionSession(
        story_id=story_id,
        workspace_root=workspace_root,
        start_time=time.time()
    )

    # Find test cases for this story
    applicable_test_cases = _find_applicable_tests(test_cases, story_id)

    if not applicable_test_cases:
        session.overall_status = "No Tests"
        session.end_time = time.time()
        return session

    # Execute all test cases for this story
    # For efficiency, we run the full test suite once and map results
    # to the story's test cases
    for test_case in applicable_test_cases:
        result = _execute_single_test_case(test_case, workspace_root, story_id)
        session.execution_results.append(result)
        session.files_changed = _resolve_test_files(workspace_root)

    # Determine overall session status
    if not session.execution_results:
        session.overall_status = "Not Started"
    else:
        # Check for any failures or blocking issues
        has_failures = any(r.execution_status == TEST_EXECUTION_FAIL for r in session.execution_results)
        has_blocked = any(r.execution_status == TEST_EXECUTION_BLOCKED for r in session.execution_results)
        has_passed = any(r.execution_status == TEST_EXECUTION_PASS for r in session.execution_results)

        if has_failures:
            session.overall_status = "Failed"
        elif has_blocked:
            session.overall_status = "Blocked"
        elif has_passed:
            session.overall_status = "Completed"
        else:
            session.overall_status = "Not Started"

    session.end_time = time.time()
    return session


def create_execution_summary(session: TestExecutionSession) -> str:
    """Create a human-readable summary of execution results."""
    if not session.execution_results:
        return f"Story {session.story_id}: No execution results recorded"

    total = len(session.execution_results)
    passed = len([r for r in session.execution_results if r.execution_status == TEST_EXECUTION_PASS])
    failed = len([r for r in session.execution_results if r.execution_status == TEST_EXECUTION_FAIL])
    blocked = len([r for r in session.execution_results if r.execution_status == TEST_EXECUTION_BLOCKED])
    not_run = len([r for r in session.execution_results if r.execution_status == TEST_EXECUTION_NOT_RUN])

    duration = session.end_time - session.start_time
    minutes = int(duration // 60)
    seconds = int(duration % 60)

    return (
        f"Story {session.story_id}: {passed} passed, {failed} failed, {blocked} blocked, {not_run} not run "
        f"in {minutes}m {seconds}s"
    )


def approve_test_execution(evidence: TestExecutionEvidence, approver_notes: str = "") -> TestExecutionEvidence:
    """Human approval step for test execution evidence.

    This is required before execution evidence is consumed by Sprint Completion.
    """
    evidence.approved = True
    evidence.human_approval = True
    evidence.human_approval_notes = approver_notes
    evidence.delivery_ready = True

    # Log the approval
    logger.info(
        f"Test execution evidence for story {evidence.story_id} approved. "
        f"Approver notes: {approver_notes}"
    )

    return evidence


def _create_test_case_from_execution(execution_result: ExecutionResult, original_test_case: TestCase) -> TestCase:
    """Create a TestCase object with execution data filled in.

    This bridges the execution layer with the existing test case models.
    """
    # Update the original test case with execution data
    updated_test_case = TestCase(
        test_id=execution_result.test_id,
        story_reference=execution_result.story_reference,
        scenario=original_test_case.scenario,
        preconditions=original_test_case.preconditions,
        steps=original_test_case.steps,
        expected_result=original_test_case.expected_result,
        priority=original_test_case.priority,
        test_type=original_test_case.test_type,
        is_approved=original_test_case.is_approved,
        execution_status=execution_result.execution_status,
        actual_result=execution_result.actual_result,
        notes=execution_result.notes,
        defect_reference=execution_result.defect_reference,
    )

    return updated_test_case


def generate_execution_evidence(story_id: str, test_cases: List[TestCase], workspace_root: str) -> TestExecutionEvidence:
    """Generate test execution evidence for a story.

    This is the main entry point for creating execution evidence that will be
    consumed by Sprint Completion.
    """
    # Create test execution session
    session = execute_story_tests(story_id, test_cases, workspace_root)

    # Create summary
    summary = create_execution_summary(session)

    # Convert execution results back to test case models for integration
    # This maintains compatibility with existing sprint_completion.py expectations
    updated_test_suites: List[TestSuite] = []

    for test_case in test_cases:
        # Find execution result for this test case
        execution_result = next(
            (er for er in session.execution_results if er.test_id == test_case.test_id and er.story_reference == story_id),
            None
        )

        if execution_result:
            # Create updated test case with execution data
            updated_test_case = _create_test_case_from_execution(execution_result, test_case)

            # Add to test suite
            suite_exists = next((s for s in updated_test_suites if s.story_id == story_id), None)
            if suite_exists:
                suite_exists.test_cases.append(updated_test_case)
            else:
                new_suite = TestSuite(story_id=story_id, test_cases=[updated_test_case])
                updated_test_suites.append(new_suite)
        else:
            # No execution result found - mark as not run
            updated_test_case = TestCase(
                test_id=test_case.test_id,
                story_reference=story_id,
                scenario=test_case.scenario,
                preconditions=test_case.preconditions,
                steps=test_case.steps,
                expected_result=test_case.expected_result,
                priority=test_case.priority,
                test_type=test_case.test_type,
                is_approved=test_case.is_approved,
                execution_status=TEST_EXECUTION_NOT_RUN,
                actual_result="No execution result available",
                notes="Test execution not performed for this story",
                defect_reference=""
            )

            suite_exists = next((s for s in updated_test_suites if s.story_id == story_id), None)
            if suite_exists:
                suite_exists.test_cases.append(updated_test_case)
            else:
                new_suite = TestSuite(story_id=story_id, test_cases=[updated_test_case])
                updated_test_suites.append(new_suite)

    # Create evidence package
    evidence = TestExecutionEvidence(
        story_id=story_id,
        session=session,
        summary=summary,
        approved=False,  # Not approved until human approval
        delivery_ready=False
    )

    return evidence


# For backward compatibility with existing sprint_completion.py expectations
def create_execution_test_suites(evidence: TestExecutionEvidence) -> List[TestSuite]:
    """Create TestSuite objects from execution evidence for Sprint Completion.

    This is a bridge function that converts our new execution evidence format
    into the TestSuite/TestCase objects that sprint_completion.py expects.
    """
    # Convert execution evidence to TestSuite/TestCase format
    updated_test_suites = []

    for test_case in evidence.session.execution_results:
        # Create a proper TestCase with execution data
        test_case_obj = TestCase(
            test_id=test_case.test_id,
            story_reference=test_case.story_reference,
            scenario="Test scenario from execution result",  # Placeholder
            preconditions="Test preconditions from execution result",  # Placeholder
            steps="Test steps from execution result",  # Placeholder
            expected_result="Test expected result from execution result",  # Placeholder
            priority="Medium",  # Default
            test_type="Functional",  # Default
            is_approved=True,  # Approved since it's executed
            execution_status=test_case.execution_status,
            actual_result=test_case.actual_result,
            notes=test_case.notes,
            defect_reference=test_case.defect_reference,
        )

        # Add to test suite
        suite_exists = next((s for s in updated_test_suites if s.story_id == test_case.story_reference), None)
        if suite_exists:
            suite_exists.test_cases.append(test_case_obj)
        else:
            from test_case_models import TestSuite
            new_suite = TestSuite(story_id=test_case.story_reference, test_cases=[test_case_obj])
            updated_test_suites.append(new_suite)

    return updated_test_suites


def get_execution_status_summary(evidence: TestExecutionEvidence) -> dict:
    """Get a summary of execution status for UI display.

    Returns counts of different execution statuses for easy UI rendering.
    """
    total = len(evidence.session.execution_results)
    if total == 0:
        return {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "blocked": 0,
            "not_run": 0,
            "status": "No results"
        }

    passed = len([r for r in evidence.session.execution_results if r.execution_status == TEST_EXECUTION_PASS])
    failed = len([r for r in evidence.session.execution_results if r.execution_status == TEST_EXECUTION_FAIL])
    blocked = len([r for r in evidence.session.execution_results if r.execution_status == TEST_EXECUTION_BLOCKED])
    not_run = len([r for r in evidence.session.execution_results if r.execution_status == TEST_EXECUTION_NOT_RUN])

    # Determine overall status
    if failed > 0:
        status = "Failed"
    elif blocked > 0:
        status = "Blocked"
    elif passed == total:
        status = "Passed"
    else:
        status = "In Progress"

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "not_run": not_run,
        "status": status
    }