"""Who is calling, and whose data they may touch.

Phase 1 serves one person on their own machine, so authentication is a single bearer
token from ``.env``. That is a deliberate scope decision rather than a shortcut, and
it has three properties that matter more than the mechanism:

* **The token resolves to a person.** Every approval, rejection and answer is audited
  against ``api_user_id``. An API with no actor would write "somebody approved this",
  which makes the audit log decorative.
* **An unset token refuses to serve.** Not "allows everything" — the API holds S1 data
  and can put claims on a resume, so a missing token is a misconfiguration, and the
  only safe response to a misconfiguration is to stop.
* **The comparison is constant-time**, and the token never appears in a log line.

Real sessions and multiple users arrive with the Phase 6 dashboard. The shape here is
chosen so that swap is a change to this module and nothing else: routes depend on
:class:`Caller`, not on a header.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from aria_core.config import Settings
from aria_core.schemas.identity import TenantId, UserId

__all__ = ["Caller", "require_caller"]

#: The header name, not a secret. (S105 flags the assignment; it is a header.)
API_TOKEN_HEADER = "x-aria-token"  # noqa: S105


@dataclass(frozen=True, slots=True)
class Caller:
    """The authenticated person, and the scope their requests run in.

    Routes take this rather than reading headers, so the day sessions replace the
    token nothing outside :mod:`aria_api.auth` has to change.
    """

    user_id: UserId
    tenant_id: TenantId
    candidate_id: uuid.UUID

    @property
    def actor_id(self) -> str:
        """What the audit log records."""
        return str(self.user_id)


def _identity(settings: Settings) -> Caller:
    """The person the token acts as, from configuration.

    All three ids are required together. A token that authenticates but names no
    candidate could read nothing and write nothing useful, and guessing which
    candidate was meant is exactly the kind of assumption this product does not make.
    """
    missing = [
        name
        for name, value in (
            ("api_user_id", settings.api_user_id),
            ("api_tenant_id", settings.api_tenant_id),
            ("api_candidate_id", settings.api_candidate_id),
        )
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"the API has no identity configured ({', '.join(missing)}); "
                "run `./tasks.ps1 seed-me` to create one"
            ),
        )
    try:
        return Caller(
            user_id=UserId(uuid.UUID(settings.api_user_id)),
            tenant_id=TenantId(uuid.UUID(settings.api_tenant_id)),
            candidate_id=uuid.UUID(settings.api_candidate_id),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the API identity in the environment is not a set of UUIDs",
        ) from error


async def require_caller(
    request: Request,
    x_aria_token: Annotated[str | None, Header(alias=API_TOKEN_HEADER)] = None,
) -> Caller:
    """Authenticate the request, or refuse it.

    Reads the configured settings off the app so a test can build an app with its own
    token without touching the environment.
    """
    settings: Settings = request.app.state.settings
    expected = settings.api_service_token
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the API has no token configured and will not serve requests",
        )
    if x_aria_token is None or not secrets.compare_digest(x_aria_token, expected.get_secret_value()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    return _identity(settings)


#: The dependency routes annotate with.
CallerDependency = Annotated[Caller, Depends(require_caller)]
