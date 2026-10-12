"""How much ink a box holds: weight for documents whose font names do not say.

RESUME_ENGINE §5.1: when no font name in a document carries weight information, bold
is judged by comparing a run's ink against runs of the same size. Rendering the page
with PDFium and counting dark pixels inside the run's box is that comparison. Measured
on a faux-bold line (outlines stroked, the font name unchanged) against regular lines
in the same font: 0.33 ink against 0.22, a margin wide enough for a 1.25x threshold.

Pages are rendered lazily and once each. This runs inside the parse sandbox, like
every other read of the file.
"""

from __future__ import annotations

from typing import Any

import pypdfium2  # type: ignore[import-untyped]  # ships no stubs

__all__ = ["PageInk"]

#: Render resolution relative to 72 dpi. Three makes a 10 pt glyph about 30 px tall:
#: enough to tell a stroke's weight, small enough to stay well inside the sandbox's
#: memory for a two-page resume.
_SCALE = 3.0
#: A pixel darker than this (of 255) is ink.
_INK_THRESHOLD = 128


class PageInk:
    """Ink share of a box on a page, from one grayscale render per page."""

    def __init__(self, content: bytes) -> None:
        self._document = pypdfium2.PdfDocument(content)
        self._renders: dict[int, Any] = {}

    def close(self) -> None:
        self._renders.clear()
        self._document.close()

    def __enter__(self) -> PageInk:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _render(self, page: int) -> Any:
        if page not in self._renders:
            bitmap = self._document[page - 1].render(scale=_SCALE, grayscale=True)
            self._renders[page] = bitmap.to_pil()
        return self._renders[page]

    def share(self, page: int, x0: float, top: float, x1: float, bottom: float) -> float:
        """The fraction of the box's pixels that are ink, 0.0 for an empty box."""
        image = self._render(page)
        box = (int(x0 * _SCALE), int(top * _SCALE), max(int(x1 * _SCALE), 1), max(int(bottom * _SCALE), 1))
        crop = image.crop(box)
        pixels: int = crop.width * crop.height
        if pixels == 0:
            return 0.0
        histogram: list[int] = crop.histogram()
        return float(sum(histogram[:_INK_THRESHOLD])) / pixels
