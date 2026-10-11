"""ARIA's command line: a thin HTTP client over the local API.

It holds no logic of its own. Every verb is one request, so "approve" cannot come to
mean something different depending on whether a person or the dashboard asked.
"""

from __future__ import annotations

from aria_cli.client import ApiError, AriaClient

__all__ = ["ApiError", "AriaClient"]
