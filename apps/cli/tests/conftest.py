"""Keep the developer's real .env out of every CLI test.

The CLI falls back to .env for its token and URL, which is right for a person and
wrong for a test: a test that forgot to stub the fallback read the developer's real
token and sent a request to their running API. CI never noticed, because CI has no
.env. The guard is autouse so forgetting is no longer possible; a test that wants a
token or a URL sets one explicitly.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aria_cli.main._from_env_file", lambda: ("", ""))
    monkeypatch.delenv("ARIA_API_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("ARIA_API_URL", raising=False)
