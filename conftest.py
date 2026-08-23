"""
Test-wide safety net: no test may make a live language-model call.

``main`` builds a ``genai.Client`` at import time whenever ``GEMINI_API_KEY`` is set,
and a developer running the suite normally has a real key in their ``.env``. Step 4 of
the Jira flow now asks that client to plan the work, so a test that renders the work
plan panel would reach the network -- slow, non-deterministic, and billed to whoever
ran ``pytest``.

Blanking ``main.CLIENT`` for every test closes that off at the source. It is not a
stub: ``jira_planner.generate_work_plan`` treats "no model available" as an ordinary
case and falls back to the deterministic mapping, which is the behaviour the existing
work-plan tests already assert. A test that wants the AI planning path exercised
injects its own ``generate`` callable, so what the planner does with a model response
is tested against fixed text rather than against whatever a live model happened to
return.

Nothing here reads, stores, or asserts on a key.
"""

import pytest


@pytest.fixture(autouse=True)
def no_live_model_calls(monkeypatch):
    """Make ``main``'s model client unavailable for the duration of every test."""
    try:
        import main
    except Exception:  # noqa: BLE001 - a suite that cannot import main has other problems
        return
    monkeypatch.setattr(main, "CLIENT", None, raising=False)
