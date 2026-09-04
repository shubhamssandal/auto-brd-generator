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

# Default model name. Reused by callers that already configured a global Gemini model;
# ``run_ai_coding_agent`` accepts an explicit override so the application can pass its
# own model id without the agent having to know about configuration modules.
DEFAULT_MODEL_NAME = "gemini-3.6-flash"

# Bounds for the bounded repository context. Kept small enough to keep the prompt
# tractable and large enough to be useful for a single story.
REPO_TREE_MAX_ENTRIES = 200
REPO_TREE_MAX_LINE_LEN = 80
REPO_FILE_EXCERPT_MAX_CHARS = 4000
REPO_FILE_EXCERPT_TOTAL_BUDGET = 16000
REPO_CONTEXT_TOTAL_BUDGET = 20000


def _get_bounded_repo_context(workspace_root: str) -> str:
    """Return a string containing a bounded view of the repository:
    - a directory tree (limited entries)
    - excerpts from source/test files (limited size)
    Excludes .git, .env*, caches, brd-env, freellmapi, and secrets.
    """
    workspace_abs = os.path.abspath(workspace_root)
    # Directories to skip entirely
    skip_dirs = {
        '.git',
        '__pycache__',
        '.pytest_cache',
        'brd-env',
        'freellmapi',
        'secrets',
    }
    # Files to skip (by prefix or exact name)
    skip_prefixes = ('.env', '.secret')
    tree_lines: List[str] = []
    file_excerpts: List[str] = []
    tree_count = 0
    excerpt_budget_remaining = REPO_FILE_EXCERPT_TOTAL_BUDGET

    for root, dirs, files in os.walk(workspace_abs):
        # Modify dirs in-place to skip unwanted directories
        rel_root = os.path.relpath(root, workspace_abs)
        if rel_root == '.':
            rel_root = ''
        # Filter dirs
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('.env')]
        for name in files:
            # Skip files with certain prefixes
            if name.startswith('.env') or name.startswith('.secret'):
                continue
            # Build relative path
            rel_path = os.path.join(rel_root, name) if rel_root else name
            # Add to tree (if we haven't exceeded limit)
            if tree_count < REPO_TREE_MAX_ENTRIES:
                # Truncate line if too long
                line = rel_path
                if len(line) > REPO_TREE_MAX_LINE_LEN:
                    line = line[:REPO_TREE_MAX_LINE_LEN - 3] + '...'
                tree_lines.append(line)
                tree_count += 1
            # If it's a Python source or test file, consider for excerpt
            if name.endswith('.py'):
                # Read excerpt if budget allows
                if excerpt_budget_remaining <= 0:
                    continue
                file_path = os.path.join(root, name)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read(REPO_FILE_EXCERPT_MAX_CHARS + 1)  # read one extra to check truncation
                except (OSError, UnicodeDecodeError):
                    continue
                if len(content) > REPO_FILE_EXCERPT_MAX_CHARS:
                    content = content[:REPO_FILE_EXCERPT_MAX_CHARS] + '... [truncated]'
                # Prepare excerpt block
                excerpt_block = f'\n### {rel_path}\n```python\n{content}\n```\n'
                if len(excerpt_block) > excerpt_budget_remaining:
                    # Truncate the block to fit remaining budget
                    excerpt_block = excerpt_block[:excerpt_budget_remaining]
                    if excerpt_block:
                        file_excerpts.append(excerpt_block)
                    excerpt_budget_remaining = 0
                else:
                    file_excerpts.append(excerpt_block)
                    excerpt_budget_remaining -= len(excerpt_block)
        # If we've exceeded both limits, we can break early (optional)
        if tree_count >= REPO_TREE_MAX_ENTRIES and excerpt_budget_remaining <= 0:
            # Still need to walk to skip directories? We'll just continue but could break.
            pass

    # Build the context string
    parts: List[str] = []
    if tree_lines:
        parts.append('Repository tree (relative to workspace root):')
        parts.extend(tree_lines)
        parts.append('')  # blank line
    if file_excerpts:
        parts.append('Source file excerpts (Python files, truncated):')
        parts.extend(file_excerpts)
    return '\n'.join(parts)

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


def _assert_safe_target_path(path: str, workspace_abs: str) -> None:
    """Raise ValueError if path is to a protected location (.git, .env*, credentials, secrets, private keys)."""
    # First check if path is valid and within workspace
    safe_path = _normalize_to_workspace(path, workspace_abs)

    # Check for protected paths - treat as escaping workspace to match test expectations
    protected_indicators = [
        '/.git/',  # .git directory anywhere
        '/.env',   # .env files
        '/.secret', # .secret files
        '/credentials/',
        '/secrets/',
        '/.ssh/',
        '/.aws/',
        '/.gcp/',
        '/.azure/',
        '/private.key',
        '.pem',
        '.key',
        '.pgp',
        '.gpg'
    ]

    # Check if path contains any protected indicators
    safe_path_lower = safe_path.lower()
    for indicator in protected_indicators:
        if indicator in safe_path_lower:
            raise ValueError(f"Path escapes workspace root: {path}")

    # Additional check for .git at root or as directory name
    if safe_path.endswith('/.git') or '/.git/' in safe_path:
        raise ValueError(f"Path escapes workspace root: {path}")

    # Check for files that start with .env or .secret
    filename = os.path.basename(safe_path)
    if filename.startswith(('.env', '.secret')):
        raise ValueError(f"Path escapes workspace root: {path}")


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

    # Validate every path up front: reject protected paths BEFORE writing any file.
    all_specs = changes.get("files_to_create", []) + changes.get("files_to_modify", [])
    for file_spec in all_specs:
        path = file_spec.get("path", "")
        if not path:
            continue
        _assert_safe_target_path(path, workspace_abs)

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


def _get_ai_response(client: Optional[Any], prompt: str, model_name: str) -> Optional[str]:
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
        response = generate(model=model_name, contents=prompt)
    except Exception as e:
        logger.error(f"AI client call failed: {e}")
        return None
    return _extract_response_text(response)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_initial_prompt(story: Story, prd_data: Optional[Any] = None, architecture_data: Optional[Any] = None, implementation_plan: Optional[Any] = None, workspace_root: Optional[str] = None) -> str:
    """Build the initial prompt for the AI coding agent."""
    # Start with basic instructions and story details
    prompt_parts = [
        "You are an AI Coding Agent. Implement the following story inside the working repository.",
        "",
        f"Story ID: {story.story_id}",
        f"Title: {story.title}",
        f"User Story: {story.user_story}",
        "",
        "Acceptance Criteria:",
    ]

    # Add acceptance criteria
    for criterion in story.acceptance_criteria:
        prompt_parts.append(f"- {criterion}")

    prompt_parts.extend([
        "",
        "Technical Tasks:",
    ])

    # Add technical tasks
    for task in story.tasks:
        prompt_parts.append(f"- {task.title}")

    # Add bounded repository context if workspace root is provided
    if workspace_root:
        repo_context = _get_bounded_repo_context(workspace_root)
        if repo_context.strip():
            prompt_parts.extend([
                "",
                "Repository Context:",
                repo_context,
            ])

    # Add PRD context if provided
    if prd_data:
        prompt_parts.extend([
            "",
            "PRD Context:",
            str(prd_data),
        ])

    # Add architecture context if provided
    if architecture_data:
        prompt_parts.extend([
            "",
            "Architecture Context:",
            str(architecture_data),
        ])

    # Add implementation plan context if provided
    if implementation_plan:
        prompt_parts.extend([
            "",
            "Implementation Plan Context:",
            str(implementation_plan),
        ])

    # Add final instructions
    prompt_parts.extend([
        "",
        "Return ONLY a JSON object (no prose, no markdown) with these keys:",
        '- "files_to_create": [{"path": "relative/path.py", "content": "full file content"}]',
        '- "files_to_modify": [{"path": "relative/path.py", "new_content": "full file content"}]',
        '- "rationale": short string',
    ])

    return "\n".join(prompt_parts)


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
    model_name: Optional[str] = None,
) -> AICodingAgentResult:
    """Run the AI Coding Agent for one approved story with a bounded retry loop.

    The agent generates a JSON description of file changes, applies them inside the
    workspace, runs the workspace's tests, and retries up to MAX_FIX_ATTEMPTS times
    if tests fail. After the cap the agent returns blocked=True with explicit reason.

    ``fix_attempts`` counts corrective retries only -- the initial generation is 0.
    """
    if model_name is None:
        model_name = DEFAULT_MODEL_NAME
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
    prompt = _build_initial_prompt(story, prd_data, architecture_data, implementation_plan, workspace)

    # Initial generation (fix_attempts == 0)
    response_text = _get_ai_response(client, prompt, model_name)
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

        response_text = _get_ai_response(client, prompt, model_name)
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
