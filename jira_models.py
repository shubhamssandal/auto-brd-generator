"""
Non-secret Jira Cloud data models.

Each model keeps only the fields this app actually uses, so whatever ends up in
Streamlit session state is an explicit, reviewable list rather than whatever the
Atlassian API happened to return. No model here holds an access token, a refresh
token, an authorization code, or a credential.
"""

from dataclasses import dataclass
from typing import Any, Optional


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
