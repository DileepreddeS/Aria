"""The shape of an upload's extracted text.

One constant and one function, shared by the two sides that have to agree about them:
``services/document_parser`` appends the links block, and ``aria_core.importer`` reads
it back. Defining it in either of those would leave the other matching on a string it
does not own, and a heading that drifts by one character silently turns a candidate's
LinkedIn URL into a resume bullet.

The links are kept as *text* rather than resolved into anything. They came out of an
uploaded file, so they are untrusted input: ARIA never fetches one (see
``docs/DECISIONS.md``), and the importer only decides which profile field a URL
belongs in.
"""

from __future__ import annotations

__all__ = ["LINKS_HEADING", "split_links_block"]

#: Appended to extracted text, once, before the URLs found in PDF link annotations.
#: Says where they came from and that nothing followed them, because the text is shown
#: to the candidate as the source of their imported facts.
LINKS_HEADING = "--- LINKS (from PDF annotations; not followed) ---"


def split_links_block(text: str) -> tuple[str, tuple[str, ...]]:
    """Split extracted text into ``(body, links)``.

    The block is always last and appears at most once, so this is a right-hand split
    rather than a scan. Text with no block comes back unchanged with no links, which
    is the ordinary case for a .txt upload and for a PDF with no annotations.
    """
    head, separator, tail = text.rpartition(f"\n{LINKS_HEADING}\n")
    if not separator:
        return text, ()
    links = tuple(line.strip() for line in tail.splitlines() if line.strip())
    return head.rstrip(), links
