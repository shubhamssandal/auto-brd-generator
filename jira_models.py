"""
Non-secret Jira Cloud data models.

Each model keeps only the fields this app actually uses, so whatever ends up in
Streamlit session state is an explicit, reviewable list rather than whatever the
Atlassian API happened to return. No model here holds an access token, a refresh
token, an authorization code, or a credential.

Issue-type hierarchy is stored as Jira reported it and is never renamed into
Epic/Story/Task here: ``PRD.md`` requires that no project be assumed to use the
same hierarchy as another, so the discovered ``subtask`` flag and
``hierarchy_level`` are the whole answer.
"""

from dataclasses import dataclass
from typing import Any, Optional

# Fields a generated Jira work plan will be able to populate from a BRD, used by
# ``JiraIssueType`` to decide whether an issue type is plannable at all.
#
# This is a statement about *this app*, not about Jira: it is the set of field ids
# a requirement can be turned into without asking the user for anything Jira did
# not already tell us. `project` and `issuetype` are implied by the selection the
# user has already made; `summary` and `description` come from the requirement;
# `parent` is set by the plan's own hierarchy. Anything else Jira marks required
# -- a custom field, a component, a due date, a fix version -- has no source in a
# BRD, so an issue type demanding one cannot be planned without more work.
#
# Deliberately small. Growing it silently would turn a validation failure into a
# runtime creation failure in a later ticket.
SUPPORTED_REQUIRED_FIELDS = frozenset(
    {"project", "issuetype", "summary", "description", "parent"}
)


@dataclass(frozen=True)
class JiraSite:
    """
    One Jira Cloud site the connected Atlassian account granted access to.

    Built from a single entry of Atlassian's ``/oauth/token/accessible-resources``
    response. ``id`` is the site's cloud id, which is the value later tickets need
    to address that site's REST API; ``scopes`` is what the token was actually
    granted on this site, which is worth showing because the endpoint documents
    that it "won't tell you anything about the user's permissions".

    ``avatarUrl`` is present in the API response and deliberately not stored --
    nothing in this app renders it, so keeping it would be session state held for
    no reason.

    Frozen so a cached selection cannot be mutated in place by a later render.
    """

    id: str
    name: str
    url: str
    scopes: tuple = ()

    @property
    def display_label(self) -> str:
        """Label for a picker. Contains no token material."""
        if self.name and self.url:
            return "{} — {}".format(self.name, self.url)
        return self.name or self.url or self.id

    @classmethod
    def from_api(cls, payload: Any) -> Optional["JiraSite"]:
        """
        Build a site from one accessible-resources entry.

        Returns ``None`` when the entry carries no id: a site that cannot be
        addressed is unusable, and returning a half-built record would let an
        unusable site reach the picker. The caller decides how to report it.
        """
        if not isinstance(payload, dict):
            return None

        site_id = str(payload.get("id") or "").strip()
        if not site_id:
            return None

        raw_scopes = payload.get("scopes")
        if isinstance(raw_scopes, list):
            scopes = tuple(str(s).strip() for s in raw_scopes if str(s).strip())
        else:
            scopes = ()

        return cls(
            id=site_id,
            name=str(payload.get("name") or "").strip(),
            url=str(payload.get("url") or "").strip(),
            scopes=scopes,
        )


def _clean(value: Any) -> str:
    """Trimmed string form of an API value, with ``None`` becoming ``""``."""
    return str(value or "").strip()


@dataclass(frozen=True)
class JiraProject:
    """
    One Jira project the connected account can see on the selected site.

    Only what this app uses: ``id`` and ``key`` to address the project's REST
    resources, ``name`` for the picker, and ``project_type_key`` because a site
    can hold a software project and a service-desk project with confusingly
    similar names and the type is what tells them apart.

    Deliberately dropped from the API response: ``avatarUrls``, ``isPrivate``,
    ``entityId``, ``uuid``, ``projectCategory``, ``lead``, ``properties``,
    ``self``, ``simplified`` and ``style``. Nothing renders them, and the
    hierarchy question they hint at is answered properly by the discovered issue
    types rather than by guessing from a project style.
    """

    id: str
    key: str
    name: str
    project_type_key: str = ""

    @property
    def display_label(self) -> str:
        """Label for a picker. Contains no token material."""
        if self.name and self.key:
            return "{} ({})".format(self.name, self.key)
        return self.name or self.key or self.id

    @property
    def api_identifier(self) -> str:
        """
        The value for a ``{projectIdOrKey}`` path segment.

        The numeric id is preferred: a project key can be renamed by an
        administrator mid-session, while the id cannot.
        """
        return self.id or self.key

    @classmethod
    def from_api(cls, payload: Any) -> Optional["JiraProject"]:
        """
        Build a project from one entry of a project-search response.

        Returns ``None`` when the entry carries neither an id nor a key, since
        such a project cannot be addressed and must not reach the picker.
        """
        if not isinstance(payload, dict):
            return None

        project_id = _clean(payload.get("id"))
        key = _clean(payload.get("key"))
        if not project_id and not key:
            return None

        return cls(
            id=project_id,
            key=key,
            name=_clean(payload.get("name")),
            project_type_key=_clean(payload.get("projectTypeKey")),
        )


@dataclass(frozen=True)
class JiraField:
    """
    One field Jira reported on an issue type's create screen.

    ``required`` is Jira's own answer for this project and issue type, not a
    guess. ``has_default_value`` matters just as much: a required field Jira
    fills in by itself does not block anything.

    ``allowedValues`` and ``autoCompleteUrl`` are present in the API response and
    not kept -- they can be large, and nothing in this ticket offers a value
    picker.
    """

    field_id: str
    name: str
    required: bool = False
    schema_type: str = ""
    has_default_value: bool = False

    @property
    def is_satisfiable(self) -> bool:
        """
        Whether a generated work plan could supply this field.

        True when the field is one this app knows how to fill, or when Jira
        supplies a default so nothing has to be sent at all.
        """
        return self.field_id in SUPPORTED_REQUIRED_FIELDS or self.has_default_value

    @classmethod
    def from_api(cls, payload: Any, fallback_id: str = "") -> Optional["JiraField"]:
        """
        Build a field from one create-metadata field entry.

        ``fallback_id`` covers the shape where fields arrive as a map keyed by
        field id and the id is therefore not repeated inside the value.
        """
        if not isinstance(payload, dict):
            return None

        field_id = (
            _clean(payload.get("fieldId"))
            or _clean(payload.get("key"))
            or _clean(fallback_id)
        )
        if not field_id:
            return None

        schema = payload.get("schema")
        schema_type = _clean(schema.get("type")) if isinstance(schema, dict) else ""

        return cls(
            field_id=field_id,
            name=_clean(payload.get("name")),
            required=bool(payload.get("required")),
            schema_type=schema_type,
            has_default_value=bool(payload.get("hasDefaultValue")),
        )


@dataclass(frozen=True)
class JiraIssueType:
    """
    One issue type the connected account could create in the selected project.

    Hierarchy is recorded exactly as Jira reported it -- ``subtask`` and
    ``hierarchy_level`` -- and is never translated into names like "Epic" or
    "Story". ``PRD.md`` is explicit that not every project uses the same
    hierarchy, so any such mapping would be this app inventing structure that the
    target project may not have.

    ``required_fields`` holds only the fields Jira marked required, because the
    full create screen can run to dozens of optional fields that nothing here
    uses. ``fields_known`` records whether that list was actually retrieved, so a
    failed lookup is reported as unvalidated rather than as "nothing required".
    """

    id: str
    name: str
    subtask: bool = False
    hierarchy_level: Optional[int] = None
    description: str = ""
    required_fields: tuple = ()
    fields_known: bool = False

    @property
    def unsupported_required_fields(self) -> tuple:
        """Required fields a generated work plan could not supply."""
        return tuple(field for field in self.required_fields if not field.is_satisfiable)

    @property
    def validation_state(self) -> str:
        """
        ``"ok"``, ``"blocked"`` or ``"unknown"``.

        Three states rather than two on purpose: "we asked Jira and every required
        field is coverable" and "we could not ask" are different answers, and
        collapsing them would report an unchecked issue type as validated.
        """
        if not self.fields_known:
            return "unknown"
        return "blocked" if self.unsupported_required_fields else "ok"

    @property
    def hierarchy_note(self) -> str:
        """
        What Jira said about this type's place in the hierarchy, verbatim in
        substance. No level is given a name.
        """
        parts = []
        if self.hierarchy_level is not None:
            parts.append("Jira hierarchy level {}".format(self.hierarchy_level))
        parts.append("subtask type" if self.subtask else "not a subtask type")
        return ", ".join(parts)

    @classmethod
    def from_api(cls, payload: Any) -> Optional["JiraIssueType"]:
        """
        Build an issue type from one create-metadata issue-type entry.

        Returns ``None`` without an id: the id is what the field lookup for this
        issue type is addressed by, so an entry lacking one is unusable.
        """
        if not isinstance(payload, dict):
            return None

        type_id = _clean(payload.get("id"))
        if not type_id:
            return None

        raw_level = payload.get("hierarchyLevel")
        level = raw_level if isinstance(raw_level, int) and not isinstance(raw_level, bool) else None

        return cls(
            id=type_id,
            name=_clean(payload.get("name")),
            subtask=bool(payload.get("subtask")),
            hierarchy_level=level,
            description=_clean(payload.get("description")),
        )


@dataclass(frozen=True)
class JiraProjectMetadata:
    """
    What the selected project would require to create an issue in it.

    Read-only, and only the parts a later ticket needs in order to check a plan
    before offering to create anything: the issue types Jira offers on this
    project's create screen, and each type's required fields.

    ``notes`` carries anything that could not be retrieved, and ``truncated``
    says whether a cap stopped the walk. Both exist so an incomplete answer is
    reported as incomplete instead of silently looking like the whole picture.
    """

    project_identifier: str
    issue_types: tuple = ()
    notes: tuple = ()
    truncated: bool = False

    @property
    def plannable_issue_types(self) -> tuple:
        """Issue types whose every required field a work plan could supply."""
        return tuple(t for t in self.issue_types if t.validation_state == "ok")

