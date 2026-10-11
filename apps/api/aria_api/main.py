"""``python -m aria_api.main`` — run the API on loopback.

``app`` is also exposed as a module attribute, so ``uvicorn aria_api.main:app
--reload`` works for development. Both paths go through the same factory, which is
what keeps the start-up checks from being skipped by whichever way it was started.

The bind address comes from configuration, which refuses anything that is not a
loopback address. There is no flag to override it here: the check belongs to the
setting, not to the way the process happens to be launched.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from aria_api.app import create_app
from aria_core.config import get_settings
from aria_core.logging.setup import configure_logging

__all__ = ["app", "main"]

#: For ``uvicorn aria_api.main:app``.
app: FastAPI = create_app()


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    uvicorn.run(
        create_app(settings),
        host=settings.api_bind_host,
        port=settings.api_bind_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
