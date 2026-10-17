"""The template's text-layer rules, checked against the stylesheet and the template source.

Each rule exists because Chromium does something to the PDF text layer that an ATS, or our
own Stage 0, cannot recover from (RESUME_ENGINE §10, verified against Chromium 2026-10-11).
A violation is found here, before rendering, rather than as a scrambled resume after it.
The rendered PDF is checked again by the ATS validator (§9 6a); this catches the cause.
"""

from __future__ import annotations

import re

__all__ = ["lint", "lint_stylesheet", "lint_template"]

_LIGATURES_OFF = re.compile(
    r'font-feature-settings\s*:\s*"liga"\s+0\s*,\s*"clig"\s+0\s*,\s*"dlig"\s+0', re.IGNORECASE
)


def _without_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _rule_body(css: str, selector: str) -> str:
    match = re.search(rf"(?:^|\}})\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    return match.group(1) if match else ""


def lint_stylesheet(css: str) -> list[str]:
    """Problems with a stylesheet, each saying what it breaks."""
    rules = _without_comments(css)
    found: list[str] = []
    if re.search(r"(?<![-\w])float\s*:", rules):
        found.append("float: Chromium writes floated boxes into the text layer ahead of the body")
    for value in re.findall(r"list-style(?:-type)?\s*:\s*([^;}]+)", rules):
        if value.strip() != "none":
            found.append("a list-style marker: CSS markers never reach the text layer")
    if not _LIGATURES_OFF.search(rules):
        found.append('ligatures not off (font-feature-settings: "liga" 0, "clig" 0, "dlig" 0)')
    if re.search(r"position\s*:\s*(?:fixed|absolute)", rules):
        found.append("fixed or absolute positioning: text out of reading order, or in a header")
    if re.search(r"@(?:top|bottom|left|right)-", rules):
        found.append("a page margin box: contact details belong in the body, not a header or footer")
    if re.search(r"url\((?!data:)", rules):
        found.append("an external url(): the page must need nothing from the network")
    what = re.search(r"width\s*:\s*(\d+(?:\.\d+)?)%", _rule_body(rules, ".what"))
    if what is None or float(what.group(1)) < 55:
        found.append("the date band starts before 55% of the content width, where Stage 0 reads dates")
    return found


def lint_template(template: str) -> list[str]:
    """Problems with the HTML template."""
    found: list[str] = []
    markup = re.sub(r"\{#.*?#\}", "", template, flags=re.DOTALL)
    for tag, why in (
        ("ul", "a list: its markers never reach the text layer"),
        ("ol", "a list: its markers never reach the text layer"),
        ("li", "a list item: its marker never reaches the text layer"),
        ("table", "a table for layout"),
        ("img", "an image: text in an image is not text"),
        ("svg", "an image: text in an image is not text"),
        ("script", "a script"),
        ("header", "a header element: contact details belong in the body"),
        ("footer", "a footer element"),
    ):
        if re.search(rf"<{tag}\b", markup, re.IGNORECASE):
            found.append(f"<{tag}>: {why}")
    contact = re.search(r'<div class="contact">(.*?)</div>', markup, re.DOTALL)
    if contact is None:
        found.append("no contact line")
    elif "<" in contact.group(1):
        found.append("markup inside the contact line: Chromium then places each glyph on its own")
    if re.search(r"\|\s*safe\b", markup) or "autoescape false" in markup:
        found.append("escaping switched off: candidate and JD text is never markup")
    return found


def lint(css: str, template: str) -> list[str]:
    return lint_stylesheet(css) + lint_template(template)
