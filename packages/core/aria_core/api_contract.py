"""What the API and its clients agree on over the wire.

Lives in core because both sides depend on core and neither may depend on the other:
the CLI is an HTTP client, and importing the API package for a header name would make
it depend on FastAPI and everything behind it.
"""

from __future__ import annotations

__all__ = ["API_TOKEN_HEADER"]

#: The header carrying the API's bearer token. The name, not a secret. (S105 flags the
#: assignment; it is a header.)
API_TOKEN_HEADER = "x-aria-token"  # noqa: S105
