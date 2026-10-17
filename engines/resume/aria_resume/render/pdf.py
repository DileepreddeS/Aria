"""Printing the page: headless Chromium through Playwright (DECISIONS 2026-10-07, 2026-10-16).

The page is our own template filled with the candidate's approved text and a JD's spelling
of a few skills. None of it should run or fetch anything, so neither can: JavaScript is
off, the context is offline, and every request is refused. A refused request is an error,
not a quiet omission, because a page that asks for something has a template bug or text
that got past escaping.
"""

from __future__ import annotations

from typing import Protocol

from playwright.async_api import Route, async_playwright

__all__ = ["ChromiumPrinter", "PdfPrinter", "RenderError"]


class RenderError(Exception):
    """The page could not be printed as it must be."""


class PdfPrinter(Protocol):
    async def print_pdf(self, html: str) -> bytes: ...


class ChromiumPrinter:
    """Prints one HTML page to PDF; page size and margins come from the stylesheet's @page."""

    async def print_pdf(self, html: str) -> bytes:
        refused: list[str] = []

        async def refuse(route: Route) -> None:
            refused.append(route.request.url)
            await route.abort()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(java_script_enabled=False, offline=True)
                await context.route("**/*", refuse)
                page = await context.new_page()
                await page.set_content(html, wait_until="load")
                pdf = await page.pdf(prefer_css_page_size=True, print_background=False)
            finally:
                await browser.close()
        if refused:
            raise RenderError(
                f"the page asked the network for {len(refused)} resource(s); nothing may be fetched"
            )
        return pdf
