"""
AI Coding Agent: Implements one approved Sprint Story end-to-end.

Provides deterministic, isolated repository operations with a bounded repair loop.
"""
import os
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict, Any

from implementation_plan_models import Story, ImplementationPlan
from test_case_models import TestSuite, TestCase, TEST_EXECUTION_PASS, TEST_EXECUTION_FAIL, TEST_EXECUTION_BLOCKED

logger = logging.getLogger(__name__)

# Maximum corrective retries after the initial generation fails tests.
MAX_FIX_ATTEMPTS = 3

from google import genai  # noqa: F401  (referenced by test patches)
from google.genai import types  # noqa: F401


@dataclass
class CodeChange:
    file_path: str
    change_type: str  # "created", "modified", "deleted"
    diff_summary: str = ""


@dataclass
class AICodingAgentResult:
    story_id: str
    files_changed: List[CodeChange] = field(default_factory=list)
    test_suites: List[TestSuite] = field(default_factory=list)
    fix_attempts: int = 0
    blocked: bool = False
    blocked_reason: str = ""
    evidence_generated: bool = False


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _normalize_to_workspace(path: str, workspace_root: str) -> str:
    """Resolve a path against the workspace, rejecting absolute paths and escapes."""
    if not path:
        raise ValueError("Empty path")
    if os.path.isabs(path):
        raise ValueError(f"Absolute paths are not allowed: {path}")
    workspace_abs = os.path.abspath(workspace_root)
    joined = os.path.normpath(os.path.join(workspace_abs, path))
    if not joined.startswith(workspace_abs + os.sep) and joined != workspace_abs:
        raise ValueError(f"Path escapes workspace root: {path}")
    return joined


def _is_safe_path(path: str, workspace_root: str) -> bool:
    try:
        _normalize_to_workspace(path, workspace_root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# AI response extraction
# ---------------------------------------------------------------------------


def _extract_response_text(response: Any) -> Optional[str]:
    """Extract a string from a model response object. Returns None if invalid."""
    if response is None:
        return None
    text = getattr(response, "text", None)
    if text is None and isinstance(response, dict):
        text = response.get("text")
    if text is None:
        return None
    if not isinstance(text, str):
        return None
    return text


def _parse_ai_response(response_text: str) -> Dict[str, Any]:
    """Parse AI response text into a structured changes dictionary."""
    if not response_text or not isinstance(response_text, str):
        return {}

    text = response_text.strip()
    if not text:
        return {}

    # Try direct JSON parse first
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    # Try to extract JSON from markdown code blocks
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    # Try to find a balanced JSON object in the text
    start = text.find('{')
    while start != -1:
        depth = 0
        for end in range(start, len(text)):
            if text[end] == '{':
                depth += 1
            elif text[end] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:end + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except (json.JSONDecodeError, TypeError):
                        pass
                    break
        start = text.find('{', start + 1)

    return {}


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------


def _apply_file_changes(changes: Dict[str, Any], workspace_root: str) -> List[CodeChange]:
    """Apply file changes inside the workspace. Raises ValueError for unsafe paths."""
    changes_made: List[CodeChange] = []
    workspace_abs = os.path.abspath(workspace_root)

    # Validate every path up front
    all_specs = changes.get("files_to_create", []) + changes.get("files_to_modify", [])
    for file_spec in all_specs:
        path = file_spec.get("path", "")
        if not path:
            continue
        _normalize_to_workspace(path, workspace_abs)

    # Create files
    for file_spec in changes.get("files_to_create", []):
        path = file_spec.get("path", "")
        content = file_spec.get("content", "")
        if not path:
            continue

        safe_path = _normalize_to_workspace(path, workspace_abs)
        dir_path = os.path.dirname(safe_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        with open(safe_path, 'w', encoding='utf-8') as f:
            f.write(content)

        changes_made.append(CodeChange(
            file_path=path,
            change_type="created",
            diff_summary=f"Created new file ({len(content)} chars)"
        ))
        logger.info(f"Created file: {safe_path}")

    # Modify files
    for file_spec in changes.get("files_to_modify", []):
        path = file_spec.get("path", "")
        new_content = file_spec.get("new_content", "")
        if not path or new_content is None:
            continue

        safe_path = _normalize_to_workspace(path, workspace_abs)
        original_content = ""
        if os.path.exists(safe_path):
            with open(safe_path, 'r', encoding='utf-8') as f:
                original_content = f.read()

        with open(safe_path, 'w', encoding='utf-8') as f:
            f.write(new_content)

        changes_made.append(CodeChange(
            file_path=path,
            change_type="modified",
            diff_summary=f"Modified file ({len(original_content)} -> {len(new_content)} chars)"
        ))
        logger.info(f"Modified file: {safe_path}")

    return changes_made


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------


def _resolve_test_files(workspace_root: str) -> List[str]:
    """Find Python test files inside the workspace."""
    test_files: List[str] = []
    workspace_abs = os.path.abspath(workspace_root)
    for root, _dirs, files in os.walk(workspace_abs):
        # skip cache directories
        if "__pycache__" in root or ".pytest_cache" in root:
            continue
        for name in files:
            if name.startswith("test_") and name.endswith(".py"):
                test_files.append(os.path.join(root, name))
    return sorted(test_files)


def _run_tests_in_workspace(workspace_root: str, story_id: str) -> Tuple[List[TestSuite], bool]:
    """Run pytest against the workspace's test files."""
    test_suites: List[TestSuite] = []
    all_passed = True

    workspace_abs = os.path.abspath(workspace_root)
    test_files = _resolve_test_files(workspace_abs)

    try:
        cmd = [sys.executable, "-m", "pytest", *test_files, "--tb=short", "-p", "no:cacheprovider"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=workspace_abs,
        )

        if result.returncode == 0:
            test_case = TestCase(
                test_id=f"{story_id}_verification",
                story_reference=story_id,
                scenario=f"Verify implementation of story {story_id}",
                preconditions="Test files exist",
                steps="Run pytest on test files",
                expected_result="All tests pass",
                priority="High",
                test_type="Functional",
                execution_status=TEST_EXECUTION_PASS,
                actual_result="All tests passed",
            )
        else:
            stderr_excerpt = (result.stderr or "")[-200:]
            test_case = TestCase(
                test_id=f"{story_id}_verification",
                story_reference=story_id,
                scenario=f"Verify implementation of story {story_id}",
                preconditions="Test files exist",
                steps="Run pytest on test files",
                expected_result="All tests pass",
                priority="High",
                test_type="Functional",
                execution_status=TEST_EXECUTION_FAIL,
                actual_result=f"Tests failed: {stderr_excerpt}",
            )
            all_passed = False

        test_suites.append(TestSuite(story_id=story_id, test_cases=[test_case]))

    except subprocess.TimeoutExpired:
        test_case = TestCase(
            test_id=f"{story_id}_verification",
            story_reference=story_id,
            scenario=f"Verify implementation of story {story_id}",
            preconditions="Test files exist",
            steps="Run pytest on test files",
            expected_result="All tests pass",
            priority="High",
            test_type="Functional",
            execution_status=TEST_EXECUTION_BLOCKED,
            actual_result="Test execution timed out",
        )
        test_suites.append(TestSuite(story_id=story_id, test_cases=[test_case]))
        all_passed = False
    except Exception as e:
        test_case = TestCase(
            test_id=f"{story_id}_verification",
            story_reference=story_id,
            scenario=f"Verify implementation of story {story_id}",
            preconditions="Test files exist",
            steps="Run pytest on test files",
            expected_result="All tests pass",
            priority="High",
            test_type="Functional",
            execution_status=TEST_EXECUTION_BLOCKED,
            actual_result=f"Test execution error: {str(e)[:200]}",
        )
        test_suites.append(TestSuite(story_id=story_id, test_cases=[test_case]))
        all_passed = False

    return test_suites, all_passed


# ---------------------------------------------------------------------------
# AI client interface
# ---------------------------------------------------------------------------


def _get_ai_response(client: Optional[Any], prompt: str) -> Optional[str]:
    """Call the AI client and return its response text, or None if unavailable."""
    if client is None:
        return None
    models = getattr(client, "models", None)
    if models is None:
        return None
    generate = getattr(models, "generate_content", None)
    if generate is None:
        return None
    try:
        response = generate(model="gemini-2.5-flash", contents=prompt)
    except Exception as e:
        logger.error(f"AI client call failed: {e}")
        return None
    return _extract_response_text(response)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_initial_prompt(story: Story) -> str:
    return f"""You are an AI Coding Agent. Implement the following story inside the working repository.

Story ID: {story.story_id}
Title: {story.title}
User Story: {story.user_story}

Acceptance Criteria:
{chr(10).join(f"- {c}" for c in story.acceptance_criteria)}

Technical Tasks:
{chr(10).join(f"- {t.title}" for t in story.tasks)}

Return ONLY a JSON object (no prose, no markdown) with these keys:
- "files_to_create": [{{"path": "relative/path.py", "content": "full file content"}}, ...]
- "files_to_modify": [{{"path": "relative/path.py", "new_content": "full file content"}}, ...]
- "rationale": short string
"""


def _build_failure_summary(test_suites: List[TestSuite]) -> str:
    failures: List[str] = []
    for suite in test_suites:
        for tc in suite.test_cases:
            if tc.execution_status != TEST_EXECUTION_PASS:
                failures.append(f"Test {tc.test_id}: {tc.execution_status} - {tc.actual_result}")
    return "\n".join(failures[:3])


def _build_retry_prompt(initial_prompt: str, failure_summary: str) -> str:
    return f"""{initial_prompt}

PREVIOUS ATTEMPT FAILED:
{failure_summary}

Please fix the issues and try again. Return ONLY the JSON object, no other text.
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_ai_coding_agent(
    story: Story,
    prd_data: Optional[Any] = None,
    architecture_data: Optional[Any] = None,
    implementation_plan: Optional[ImplementationPlan] = None,
    client: Optional[Any] = None,
    workspace_root: Optional[str] = None,
) -> AICodingAgentResult:
    """Run the AI Coding Agent for one approved story with a bounded retry loop.

    The agent generates a JSON description of file changes, applies them inside the
    workspace, runs the workspace's tests, and retries up to MAX_FIX_ATTEMPTS times
    if tests fail. After the cap the agent returns blocked=True with explicit reason.

    ``fix_attempts`` counts corrective retries only -- the initial generation is 0.
    """
    if not story.is_ready:
        return AICodingAgentResult(
            story_id=story.story_id,
            blocked=True,
            blocked_reason=f"Story not ready: {story.readiness_gaps}",
            evidence_generated=False,
        )

    workspace = os.path.abspath(workspace_root or os.getcwd())
    logger.info(f"Starting AI Coding Agent for story {story.story_id} in {workspace}")

    # Track unique file changes by path - stores the latest CodeChange for each file
    changes_by_path: Dict[str, CodeChange] = {}
    test_suites: List[TestSuite] = []
    prompt = _build_initial_prompt(story)

    # Initial generation (fix_attempts == 0)
    response_text = _get_ai_response(client, prompt)
    if response_text is None:
        return AICodingAgentResult(
            story_id=story.story_id,
            blocked=True,
            blocked_reason="AI client unavailable or returned no response",
            evidence_generated=False,
        )

    changes_dict = _parse_ai_response(response_text)
    if not changes_dict:
        return AICodingAgentResult(
            story_id=story.story_id,
            blocked=True,
            blocked_reason="AI client response could not be parsed as JSON",
            evidence_generated=False,
        )

    try:
        new_changes = _apply_file_changes(changes_dict, workspace)
        for change in new_changes:
            changes_by_path[change.file_path] = change
    except ValueError as e:
        return AICodingAgentResult(
            story_id=story.story_id,
            blocked=True,
            blocked_reason=str(e),
            evidence_generated=False,
        )

    test_suites, all_passed = _run_tests_in_workspace(workspace, story.story_id)
    if all_passed:
        return AICodingAgentResult(
            story_id=story.story_id,
            files_changed=list(changes_by_path.values()),
            test_suites=test_suites,
            fix_attempts=0,
            blocked=False,
            evidence_generated=True,
        )

    # Bounded corrective retries
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        failure_summary = _build_failure_summary(test_suites)
        prompt = _build_retry_prompt(prompt, failure_summary)

        response_text = _get_ai_response(client, prompt)
        if response_text is None:
            return AICodingAgentResult(
                story_id=story.story_id,
                files_changed=list(changes_by_path.values()),
                test_suites=test_suites,
                fix_attempts=attempt,
                blocked=True,
                blocked_reason=f"AI client unavailable on corrective attempt {attempt}",
                evidence_generated=len(changes_by_path) > 0,
            )

        changes_dict = _parse_ai_response(response_text)
        if not changes_dict:
            return AICodingAgentResult(
                story_id=story.story_id,
                files_changed=list(changes_by_path.values()),
                test_suites=test_suites,
                fix_attempts=attempt,
                blocked=True,
                blocked_reason=f"AI response unparseable on corrective attempt {attempt}",
                evidence_generated=len(changes_by_path) > 0,
            )

        try:
            new_changes = _apply_file_changes(changes_dict, workspace)
            for change in new_changes:
                changes_by_path[change.file_path] = change
        except ValueError as e:
            return AICodingAgentResult(
                story_id=story.story_id,
                files_changed=list(changes_by_path.values()),
                test_suites=test_suites,
                fix_attempts=attempt,
                blocked=True,
                blocked_reason=str(e),
                evidence_generated=len(changes_by_path) > 0,
            )

        test_suites, all_passed = _run_tests_in_workspace(workspace, story.story_id)
        if all_passed:
            return AICodingAgentResult(
                story_id=story.story_id,
                files_changed=list(changes_by_path.values()),
                test_suites=test_suites,
                fix_attempts=attempt,
                blocked=False,
                evidence_generated=True,
            )

    # Cap reached: BLOCKED with last evidence
    return AICodingAgentResult(
        story_id=story.story_id,
        files_changed=list(changes_by_path.values()),
        test_suites=test_suites,
        fix_attempts=MAX_FIX_ATTEMPTS,
        blocked=True,
        blocked_reason=f"Failed to pass tests after {MAX_FIX_ATTEMPTS} corrective attempts",
        evidence_generated=len(changes_by_path) > 0,
    )
