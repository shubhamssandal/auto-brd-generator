"""
Jira Cloud integration configuration.

This is the only module that knows which environment variables carry Jira
credentials. It deliberately contains no HTTP client, no OAuth flow, and no
issue-creation code: authorization arrives with the Jira OAuth ticket, and
writing to Jira is gated behind explicit user approval later in the workflow.

Credential values are read from the environment on demand. The reporting
helpers expose variable NAMES only, never values, so a missing-configuration
message or a setup panel can be rendered in the UI without leaking a secret.
"""

import os

from providers.base import ProviderNotConfiguredError

DISPLAY_NAME = "Jira Cloud"

# Names only. Values live in the local .env file, which is git-ignored.
CLIENT_ID_VAR = "JIRA_CLIENT_ID"
CLIENT_SECRET_VAR = "JIRA_CLIENT_SECRET"
# Shared with the Google and Microsoft flows rather than introducing a
# Jira-specific redirect variable.
REDIRECT_URI_VAR = "APP_REDIRECT_URI"

DEFAULT_REDIRECT_URI = "http://localhost:8501"


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def get_client_id() -> str:
    """The configured OAuth client id, or an empty string when unset."""
    return _env(CLIENT_ID_VAR)


def get_client_secret() -> str:
    """The configured OAuth client secret, or an empty string when unset."""
    return _env(CLIENT_SECRET_VAR)


def get_redirect_uri() -> str:
    """The OAuth callback URI, falling back to the local Streamlit address."""
    return _env(REDIRECT_URI_VAR) or DEFAULT_REDIRECT_URI


def is_configured() -> bool:
    """True when every credential Jira needs is present in the environment."""
    return bool(get_client_id() and get_client_secret())


def get_missing_configuration() -> list[str]:
    """Names of the environment variables that still need to be set."""
    missing = []
    if not get_client_id():
        missing.append(CLIENT_ID_VAR)
    if not get_client_secret():
        missing.append(CLIENT_SECRET_VAR)
    return missing


def require_configured() -> None:
    """
    Guard for every future Jira API call.

    Raises ProviderNotConfiguredError naming the missing variables, so callers
    fail loudly at the configuration boundary instead of sending a request with
    empty credentials.
    """
    missing = get_missing_configuration()
    if missing:
        raise ProviderNotConfiguredError(
            "{} is not configured. Missing: {}".format(DISPLAY_NAME, ", ".join(missing))
        )


def get_setup_instructions() -> str:
    """Configuration steps for the UI. Lists variable names, never values."""
    return (
        "### Jira Cloud Configuration Prerequisites\n\n"
        "1. **Create an OAuth 2.0 (3LO) app** in the Atlassian Developer console\n"
        "   for the Jira Cloud site you want to use.\n"
        "2. **Set the callback URL** on that app to the same value as\n"
        "   `{redirect}` (default: `{default}`).\n"
        "3. **Environment variable names** (set the values in your local `.env`,\n"
        "   never in code):\n"
        "   - `{client_id}`\n"
        "   - `{client_secret}`\n"
        "   - `{redirect}` (optional; shared with the other integrations)\n\n"
        "Jira remains optional: transcript ingestion and BRD generation work "
        "without any of these variables."
    ).format(
        client_id=CLIENT_ID_VAR,
        client_secret=CLIENT_SECRET_VAR,
        redirect=REDIRECT_URI_VAR,
        default=DEFAULT_REDIRECT_URI,
    )
