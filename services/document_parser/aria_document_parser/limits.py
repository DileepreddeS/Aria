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

    # ----------------------------------------------------------- the sandbox
    # These bound the worker process itself rather than the document, because a
    # hostile file attacks the extractor, not the parse logic, and some extractors are
    # native code. The container enforces them from outside; the worker also imposes
    # them on itself before reading any input, so a parse that runs without the
    # container is still bounded.

    #: Memory the worker may commit. Parsing a resume needs well under 200 MB; a
    #: decompression bomb inside a PDF stream asks for gigabytes, and this is where it
    #: stops instead of taking the machine with it.
    max_memory_bytes: int = 512 * 1024 * 1024
    #: Virtual address space on Linux, where RLIMIT_AS counts reservations as well as
    #: use: an interpreter and its shared libraries reserve far more than they touch,
    #: so this sits above max_memory_bytes rather than at it.
    max_address_space_bytes: int = 2 * 1024 * 1024 * 1024
    #: CPU seconds. The wall-clock timeout is the main defence against a loop; this is
    #: the one that still holds if the process outlives the parent that was timing it.
    max_cpu_seconds: int = 30
    #: Processes and threads in the container. A parser has no reason to start a
    #: process at all, so a fork bomb fails at once instead of exhausting the host.
    max_processes: int = 32
    #: Bytes read back from the worker on each of stdout and stderr. A real result is
    #: a few hundred kilobytes of JSON; a worker that has been subverted into writing
    #: without end must not be able to fill the API's memory instead.
    max_output_bytes: int = 8 * 1024 * 1024

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

    #: Link annotations read per page. A resume header repeats its links on every page;
    #: a file with hundreds of annotations per page is not a resume.
    max_links_per_page: int = 50

    #: Links kept in total, after de-duplication.
    max_links_total: int = 60

    #: Longest URI kept. Real profile links are well under this; a longer one is
    #: either tracking cruft or an attempt to fill a column.
    max_link_length: int = 500
