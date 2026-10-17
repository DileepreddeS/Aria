"""A ResumeDocument as one self-contained HTML page.

Jinja2 with autoescape on and undefined names an error: every string in a document is
the candidate's text or a JD's spelling of a skill, and none of it is markup. The fonts
travel inside the page as data URIs, so printing needs nothing from the network (the
printer blocks every request anyway) and the result does not depend on the machine's fonts.
"""

from __future__ import annotations

import base64
from functools import cache
from importlib import resources

from jinja2 import Environment, PackageLoader, StrictUndefined

from aria_resume.document import ResumeDocument

__all__ = ["TEMPLATE_VERSION", "render_html", "stylesheet", "template_source"]

#: Recorded in every resume's artifacts. Bump on any change to the template or stylesheet.
TEMPLATE_VERSION = "1"

_FACES = (
    ("Carlito-Regular.ttf", 400, "normal"),
    ("Carlito-Bold.ttf", 700, "normal"),
    ("Carlito-Italic.ttf", 400, "italic"),
)


@cache
def _environment() -> Environment:
    return Environment(
        loader=PackageLoader("aria_resume.render", "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


@cache
def stylesheet() -> str:
    return (resources.files("aria_resume.render") / "templates" / "resume.css").read_text(encoding="utf-8")


@cache
def template_source() -> str:
    return (resources.files("aria_resume.render") / "templates" / "resume.html.j2").read_text(
        encoding="utf-8"
    )


@cache
def _font_faces() -> tuple[dict[str, str], ...]:
    """Each face as base64, which autoescaping leaves untouched: its alphabet has no markup."""
    fonts = resources.files("aria_resume.render") / "fonts"
    return tuple(
        {
            "data": base64.b64encode((fonts / name).read_bytes()).decode("ascii"),
            "weight": str(weight),
            "style": style,
        }
        for name, weight, style in _FACES
    )


def render_html(document: ResumeDocument) -> str:
    """The page, byte-for-byte the same for the same document.

    Nothing is marked safe: the stylesheet comes in through an include, whose literal text
    autoescaping does not touch, and every value inserted is escaped.
    """
    return _environment().get_template("resume.html.j2").render(document=document, font_faces=_font_faces())
