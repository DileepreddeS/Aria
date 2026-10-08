"""``python -m aria_llm_gateway.main`` — run the gateway on loopback.

The bind address comes from configuration, which refuses anything that is not a
loopback address: the gateway is internal and is reachable only from the API on the
same host (SECURITY.md §2.5). There is no flag to override that here, because the
check belongs to the setting rather than to the way it happens to be started.
"""

from __future__ import annotations

import uvicorn

from aria_core.config import get_settings
from aria_core.logging.setup import configure_logging
from aria_llm_gateway.app import create_app


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    uvicorn.run(
        create_app(settings=settings),
        host=settings.llm_gateway_bind_host,
        port=settings.llm_gateway_bind_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
