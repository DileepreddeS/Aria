"""Hard limits on parsing (SECURITY.md §7).

A resume is a small document. Every number here is generous for a real one and
hostile to a file built to exhaust something, which is the shape every limit in this
file takes: large enough that no candidate will ever meet it, small enough that
meeting it is evidence of intent.

Each limit says what it is defending against, because a limit whose reason is
forgotten gets raised the first time someone hits it.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ParseLimits"]


@dataclass(frozen=True, slots=True)
class ParseLimits:
    """What the parser will not exceed."""

    #: Wall clock for the whole parse, enforced by killing the worker. A PDF can make
    #: an extractor loop; a timeout is the only reliable answer to that.
    timeout_seconds: float = 20.0

    #: A one-page resume is the goal and two is common. Fifty is not a resume, and a
    #: PDF claiming 20,000 pages is a decompression attack with a cover story.
    max_pages: int = 50

    #: Extracted characters. A dense two-page resume is around 6,000; 400,000 is
    #: beyond any document a person wrote about themselves.
    max_characters: int = 400_000

    #: Total uncompressed bytes allowed out of a DOCX archive. The classic zip bomb
    #: is a few kilobytes that expands to gigabytes.
    max_uncompressed_bytes: int = 60 * 1024 * 1024

    #: Uncompressed-to-compressed ratio. Ordinary office XML compresses about 10:1;
    #: a bomb is thousands to one.
    max_compression_ratio: int = 200

    #: Entries in a DOCX archive. A real document has tens.
    max_archive_entries: int = 500

    #: Below this, a PDF has no usable text layer: it is a scan. ARIA refuses those
    #: rather than running OCR, because a scanned resume is not ATS-safe either
    #: (RESUME_ENGINE §1.8) — accepting it would produce a resume that the systems it
    #: is sent to cannot read.
    min_pdf_characters: int = 200

    #: How far the two PDF extractors may differ, per page, before the page is flagged
    #: for review. They disagree slightly on whitespace and ligatures even on good
    #: files; a two-column layout read in different orders disagrees sharply.
    extractor_disagreement_ratio: float = 0.75

    #: Link annotations read per page. A resume header repeats its links on every page;
    #: a file with hundreds of annotations per page is not a resume.
    max_links_per_page: int = 50

    #: A vertical band this wide that no text crosses is a column gutter rather than
    #: word spacing. Quarter of an inch: wider than any inter-word gap, narrower than
    #: the gutter in every two-column resume template.
    min_gutter_points: float = 18.0
    #: How close to both margins a box must reach to count as spanning the whole
    #: page. A spanning header starts at the left margin and ends at the right one;
    #: the longest bullet of a left column is also wide but stops well short of the
    #: right margin, and treating it as spanning moved the gutter into the middle of
    #: it. Measured as a share of the content width.
    spanning_margin_ratio: float = 0.1
    #: The smaller column must hold this share of the page's characters. Below it, the
    #: "column" is a page number or a caption, and reordering the page around one
    #: would be worse than leaving it alone.
    min_column_share: float = 0.15
    #: Columns run beside each other. Two blocks at different x positions that barely
    #: overlap vertically are a heading and an indented paragraph.
    min_column_overlap: float = 0.5
    #: Text boxes considered per page. A page with thousands is generated, or an
    #: attempt to make layout analysis expensive.
    max_text_boxes_per_page: int = 2000

    #: Links kept in total, after de-duplication.
    max_links_total: int = 60

    #: Longest URI kept. Real profile links are well under this; a longer one is
    #: either tracking cruft or an attempt to fill a column.
    max_link_length: int = 500
