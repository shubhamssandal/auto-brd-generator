# Auto-BRD Generator — Security & Access

## Principles
- Least privilege
- User consent
- Explicit approval before write actions
- Secret isolation
- No token exposure
- No sensitive data in logs

## Secret Management
- Use environment variables or deployment secrets
- Keep `.env` out of Git
- Keep `.env.example` free of real credentials
- Never hardcode production credentials

## OAuth State
Protect authorization flows with state validation, expiration, and single-use behavior where supported. Use PKCE where required/supported.

## Token Handling
- Never display access tokens
- Never log access or refresh tokens
- Keep tokens in controlled session/server-side state
- Replace rotated refresh tokens when required
- Clear connection state on disconnect

## Google Meet
Use minimum read-only scopes required by implemented functionality. Clearly handle cases where no retrievable transcript exists.

## Microsoft Teams
Use only required Microsoft permissions and clearly surface permission or tenant restrictions.

## Jira
Initial Jira integration includes discovery permissions and controlled write access for explicitly approved issue creation.

Rules:
- AI generation never creates an issue by itself
- User reviews before creation
- Metadata and required fields are validated
- OAuth tokens are never displayed

## Write-Action Confirmation
Visually separate:
Generate Jira Plan
from:
Create Selected Issues

Creation requires explicit user action.

## Logging
Allowed: high-level events, non-sensitive error categories, created issue keys where needed.

Forbidden: access tokens, refresh tokens, client secrets, authorization codes, and unnecessary sensitive meeting content.

## Dependency Security
Before deployment, review dependencies, remove unused packages, keep credentials out of Git, and review OAuth redirect URLs.
