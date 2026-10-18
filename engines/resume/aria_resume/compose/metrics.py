"""How wide a line of text prints, from the bundled Carlito's own glyph widths.

The line budget (RESUME_ENGINE §8 5a) estimates how many printed lines each bullet takes,
so it reads the advance width of every character from the font the page is set in: the
``cmap`` table maps a character to a glyph, ``hmtx`` gives the glyph's advance, ``head``
the units per em. The font is the bundled, hash-pinned file (render/fonts/SOURCE.md), so
the reader is small and trusts nothing it does not check. Chromium also kerns, which this
ignores; the estimate keeps slack for it, and the page is checked again after rendering.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from functools import cache
from pathlib import Path

__all__ = ["FontMetrics", "carlito"]

FONTS = Path(__file__).resolve().parents[1] / "render" / "fonts"


class FontError(ValueError):
    """The font file lacks a table the reader needs, or one is malformed."""


@dataclass(frozen=True)
class FontMetrics:
    units_per_em: int
    advances: dict[int, int]
    #: The advance used for a character the font does not map: its .notdef glyph's.
    fallback: int

    def width(self, text: str, size: float) -> float:
        """Points wide, at ``size`` points, without kerning."""
        units = sum(self.advances.get(ord(character), self.fallback) for character in text)
        return units * size / self.units_per_em


def _tables(data: bytes) -> dict[bytes, tuple[int, int]]:
    if len(data) < 12:
        raise FontError("too short to be a font")
    (count,) = struct.unpack_from(">H", data, 4)
    tables: dict[bytes, tuple[int, int]] = {}
    for index in range(count):
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", data, 12 + 16 * index)
        if offset + length > len(data):
            raise FontError(f"table {tag!r} runs past the end of the file")
        tables[tag] = (offset, length)
    for required in (b"head", b"hhea", b"hmtx", b"cmap"):
        if required not in tables:
            raise FontError(f"no {required.decode()} table")
    return tables


def _cmap_format4(data: bytes, offset: int) -> dict[int, int]:
    (_format, _length, _language, double_segments) = struct.unpack_from(">HHHH", data, offset)
    segments = double_segments // 2
    base = offset + 14
    ends = struct.unpack_from(f">{segments}H", data, base)
    starts = struct.unpack_from(f">{segments}H", data, base + 2 * segments + 2)
    deltas = struct.unpack_from(f">{segments}h", data, base + 4 * segments + 2)
    range_base = base + 6 * segments + 2
    range_offsets = struct.unpack_from(f">{segments}H", data, range_base)
    mapping: dict[int, int] = {}
    for segment in range(segments):
        for code in range(starts[segment], ends[segment] + 1):
            if code == 0xFFFF:
                continue
            if range_offsets[segment] == 0:
                glyph = (code + deltas[segment]) & 0xFFFF
            else:
                address = range_base + 2 * segment + range_offsets[segment] + 2 * (code - starts[segment])
                (glyph,) = struct.unpack_from(">H", data, address)
                if glyph:
                    glyph = (glyph + deltas[segment]) & 0xFFFF
            if glyph:
                mapping[code] = glyph
    return mapping


def read_metrics(path: Path) -> FontMetrics:
    data = path.read_bytes()
    tables = _tables(data)
    (units_per_em,) = struct.unpack_from(">H", data, tables[b"head"][0] + 18)
    (metric_count,) = struct.unpack_from(">H", data, tables[b"hhea"][0] + 34)
    hmtx = tables[b"hmtx"][0]
    widths = [struct.unpack_from(">H", data, hmtx + 4 * index)[0] for index in range(metric_count)]

    cmap = tables[b"cmap"][0]
    (_version, subtables) = struct.unpack_from(">HH", data, cmap)
    mapping: dict[int, int] | None = None
    for index in range(subtables):
        platform, encoding, offset = struct.unpack_from(">HHI", data, cmap + 4 + 8 * index)
        (format_,) = struct.unpack_from(">H", data, cmap + offset)
        if platform == 3 and encoding == 1 and format_ == 4:
            mapping = _cmap_format4(data, cmap + offset)
            break
    if mapping is None:
        raise FontError("no Windows Unicode (3, 1) format 4 cmap")

    def advance(glyph: int) -> int:
        # Glyphs past the last full metric share its advance (the TrueType rule).
        return int(widths[min(glyph, metric_count - 1)])

    return FontMetrics(
        units_per_em=units_per_em,
        advances={code: advance(glyph) for code, glyph in mapping.items()},
        fallback=advance(0),
    )


@cache
def carlito(weight: str = "Regular") -> FontMetrics:
    return read_metrics(FONTS / f"Carlito-{weight}.ttf")
