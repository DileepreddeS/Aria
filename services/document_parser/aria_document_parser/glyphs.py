"""Characters with their geometry: Stage 0's raw material (RESUME_ENGINE §5.1).

pdfplumber gives every character with its box, font name and size. Reading order,
columns, lines and word boundaries are all rebuilt from these later; nothing here
trusts the order the generator happened to write characters in, or any extractor's
idea of where words begin.

**Weight comes from the font's PostScript name**, because pdfplumber reports a name
and not a bold flag. Subset fonts carry a six-letter prefix (``ABCDEF+Carlito-Bold``),
removed before matching. A name says one of three things: bold (``-Bold``,
``,Bold``, ``Semibold``, ``-Black``…), not bold (``-Regular``, ``-Italic``,
``-Medium``…), or nothing at all (``Carlito``). The third is not the same as the
second — a document whose fonts are all named without a weight leaves weight unknown,
and line building falls back to measuring ink rather than guessing regular.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["Glyph", "base_font_name", "page_glyphs", "weight_from_name"]

#: A subset font's tag: six capitals and a plus, added by whatever embedded it.
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")

#: Weights at or above semibold. When a name has a separator only the style part is
#: matched, so "Blackwood-Regular" is not bold. A name with no separator is matched
#: whole — the price of reading "ArialBold" — so a bare "Blackwood" would be.
_BOLD = re.compile(r"(semi|demi|extra|ultra)?-?(bold|black|heavy)", re.IGNORECASE)
#: Style words that say "this is a weight, and it is not bold".
_NOT_BOLD = re.compile(
    r"regular|roman|book|normal|medium|light|thin|hairline|italic|oblique|plain|text", re.IGNORECASE
)


#: TeX's fonts carry weight in the family code rather than a style word: Computer
#: Modern's CMBX10 is bold extended, CMR10 roman; the EC/cm-super equivalents are
#: SFBX and SFRM. LaTeX resumes (moderncv, deedy) are a large share of the corpus
#: (§5.9), so these are named here rather than left to the ink-density fallback.
_TEX_BOLD = re.compile(r"^(CMBX|CMSSBX|CMB|SFBX|SFSX|SFSB)\d+$")
_TEX_NOT_BOLD = re.compile(r"^(CMR|CMSS|CMTI|CMSL|CMTT|CMCSC|SFRM|SFSS|SFTI|SFTT|SFSL|SFCC)\d+$")


def base_font_name(fontname: str) -> str:
    """``ABCDEF+Carlito-Bold`` -> ``Carlito-Bold``."""
    return _SUBSET_PREFIX.sub("", fontname)


def weight_from_name(fontname: str) -> bool | None:
    """True for a bold name, False for a named non-bold weight, None when the name says nothing.

    The style is what follows the family, after ``-`` or ``,``: ``Arial-BoldMT``,
    ``Calibri,Bold``, ``Montserrat-SemiBold``. A name with no separator is checked
    whole, for the generators that write ``ArialBold``.
    """
    name = base_font_name(fontname)
    if _TEX_BOLD.match(name):
        return True
    if _TEX_NOT_BOLD.match(name):
        return False
    family, separator, style = (
        re.split(r"([-,])", name, maxsplit=1) if re.search(r"[-,]", name) else (name, "", "")
    )
    candidate = style if separator else family
    if _BOLD.search(candidate):
        return True
    if separator and _NOT_BOLD.search(candidate):
        return False
    return None


@dataclass(frozen=True, slots=True)
class Glyph:
    """One character as drawn: text, box (points, from the page's top-left), font."""

    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    font: str
    #: From the name: True, False, or None when the name carries no weight.
    bold: bool | None
    #: Rotated text is not part of a resume's reading order.
    upright: bool

    @property
    def unreadable(self) -> bool:
        """A character the font could not map to Unicode."""
        return self.text.startswith("(cid:") or "\ufffd" in self.text


def page_glyphs(page: Any) -> list[Glyph]:
    """Every character pdfplumber found on a page, in no particular order."""
    glyphs: list[Glyph] = []
    for char in page.chars:
        font = base_font_name(str(char.get("fontname", "")))
        glyphs.append(
            Glyph(
                text=str(char["text"]),
                x0=float(char["x0"]),
                x1=float(char["x1"]),
                top=float(char["top"]),
                bottom=float(char["bottom"]),
                size=float(char["size"]),
                font=font,
                bold=weight_from_name(font),
                upright=bool(char.get("upright", True)),
            )
        )
    return glyphs
