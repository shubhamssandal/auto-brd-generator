"""
JIRA-001 tests: Jira configuration isolation and secret handling.

No network access and no real Jira credentials are required. The environment is
cleared per test so results never depend on what happens to be in the local
.env file, or on which other test module imported main.py first.
"""

import pytest

import jira_config
from providers.base import ProviderNotConfiguredError

JIRA_ENV_VARS = (
    jira_config.CLIENT_ID_VAR,
    jira_config.CLIENT_SECRET_VAR,
    jira_config.REDIRECT_URI_VAR,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in JIRA_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def configure(monkeypatch, client_id="jira-id", client_secret="jira-secret"):
    monkeypatch.setenv(jira_config.CLIENT_ID_VAR, client_id)
    monkeypatch.setenv(jira_config.CLIENT_SECRET_VAR, client_secret)


# --- Configuration reporting ---

def test_is_not_configured_when_credentials_are_absent():
    assert jira_config.is_configured() is False


def test_missing_configuration_lists_both_variable_names():
    assert jira_config.get_missing_configuration() == [
        "JIRA_CLIENT_ID",
        "JIRA_CLIENT_SECRET",
    ]


def test_is_configured_when_both_credentials_are_present(monkeypatch):
    configure(monkeypatch)
    assert jira_config.is_configured() is True
    assert jira_config.get_missing_configuration() == []


def test_partial_configuration_reports_only_what_is_missing(monkeypatch):
    monkeypatch.setenv(jira_config.CLIENT_ID_VAR, "jira-id")
    assert jira_config.is_configured() is False
    assert jira_config.get_missing_configuration() == ["JIRA_CLIENT_SECRET"]


def test_whitespace_only_values_count_as_missing(monkeypatch):
    configure(monkeypatch, client_id="   ", client_secret="\t")
    assert jira_config.is_configured() is False
    assert jira_config.get_missing_configuration() == [
        "JIRA_CLIENT_ID",
        "JIRA_CLIENT_SECRET",
    ]


def test_credentials_are_read_from_the_environment(monkeypatch):
    configure(monkeypatch, client_id="  id-with-space  ", client_secret=" secret ")
    assert jira_config.get_client_id() == "id-with-space"
    assert jira_config.get_client_secret() == "secret"


# --- Guard for future API calls ---

def test_require_configured_raises_and_names_the_missing_variables():
    with pytest.raises(ProviderNotConfiguredError) as excinfo:
        jira_config.require_configured()

    message = str(excinfo.value)
    assert "JIRA_CLIENT_ID" in message
    assert "JIRA_CLIENT_SECRET" in message


def test_require_configured_is_silent_when_configured(monkeypatch):
    configure(monkeypatch)
    assert jira_config.require_configured() is None


# --- Redirect URI is shared with the existing integrations ---

def test_redirect_uri_defaults_to_localhost():
    assert jira_config.get_redirect_uri() == "http://localhost:8501"


def test_redirect_uri_honours_the_environment(monkeypatch):
    monkeypatch.setenv(jira_config.REDIRECT_URI_VAR, "https://example.test/callback")
    assert jira_config.get_redirect_uri() == "https://example.test/callback"


# --- Secrets never leave the environment ---

def test_setup_instructions_list_variable_names_but_never_values(monkeypatch):
    configure(monkeypatch, client_id="id-value-abc", client_secret="secret-value-xyz")

    instructions = jira_config.get_setup_instructions()

    assert "JIRA_CLIENT_ID" in instructions
    assert "JIRA_CLIENT_SECRET" in instructions
    assert "id-value-abc" not in instructions
    assert "secret-value-xyz" not in instructions


def test_configuration_errors_never_echo_a_configured_value(monkeypatch):
    monkeypatch.setenv(jira_config.CLIENT_ID_VAR, "id-value-abc")

    with pytest.raises(ProviderNotConfiguredError) as excinfo:
        jira_config.require_configured()

    assert "id-value-abc" not in str(excinfo.value)
    assert "id-value-abc" not in " ".join(jira_config.get_missing_configuration())


# --- No Jira write action exists yet (JIRA-001 boundary) ---

def test_configuration_module_has_no_http_client():
    """
    JIRA-001 adds configuration only. This module must not be able to reach Jira,
    so it holds no HTTP client of its own -- authorization and issue creation
    arrive in their own tickets.
    """
    assert not hasattr(jira_config, "requests")
