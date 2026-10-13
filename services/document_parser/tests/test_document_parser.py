"""The parser reads hostile files (SECURITY.md §7).

Every input here is either a real resume or something built to break an extractor.
Documents are constructed in `fixtures.py` rather than committed as binaries, so the
hostile ones can be read as well as run.
"""

from __future__ import annotations

import asyncio
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fixtures import (  # type: ignore[import-not-found]  # sibling helper, see conftest
    RESUME_LINES,
    docx_document_xml,
    image_only_pdf,
    make_docx,
    make_pdf,
    make_pdf_with_links,
    multi_page_pdf,
    two_column_pdf,
)
from pathology_pdfs import encrypted_pdf  # type: ignore[import-not-found]  # sibling helper

from aria_core.documents import CrossExtractor, Triage
from aria_document_parser import client
from aria_document_parser.client import InProcessDocumentParser, SubprocessDocumentParser
from aria_document_parser.docx import extract_docx
from aria_document_parser.errors import ParseFailed, ParseRefused
from aria_document_parser.limits import ParseLimits
from aria_document_parser.pdf import LINKS_HEADING, extract_pdf
from aria_document_parser.plain_text import extract_text_file
from aria_document_parser.worker import parse_bytes

RESUME_TEXT = "\n".join(RESUME_LINES)
FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "resumes"


# ------------------------------------------------------------------------- PDF


class TestPdf:
    def test_a_real_pdf_yields_its_text_as_lines(self) -> None:
        result = extract_pdf(make_pdf("Built 20+ React components used by 4 teams. " * 8))
        assert "React components" in result.text
        assert result.page_count == 1
        assert result.layout.triage is Triage.READABLE
        assert result.layout.lines

    def test_the_text_is_the_layout_s_lines_in_reading_order(self) -> None:
        result = extract_pdf(make_pdf())
        assert result.text == "\n".join(line.text for line in result.layout.lines)

    def test_a_bullet_keeps_its_marker_in_the_text(self) -> None:
        # The text importer recognises a bullet by its leading glyph; the layout keeps
        # the marker apart from the words, and the flat text puts it back in front.
        result = extract_pdf((FIXTURES / "markerless_bullets.pdf").read_bytes())
        marked = [line for line in result.layout.lines if line.marker]
        assert marked and all(f"{line.marker} {line.text}" in result.text.splitlines() for line in marked)

    def test_the_detail_names_the_reader_and_never_a_percentage(self) -> None:
        # §5.6: agreement is a diagnostic, never a score. "agreement 100%" over nine
        # fabricated roles is what the old reader printed.
        result = extract_pdf(make_pdf())
        assert result.extractor == "pdfplumber"
        assert "extractor=pdfplumber" in result.detail
        assert "cross-extractor: agree" in result.detail
        assert "%" not in result.detail and "agreement" not in result.detail

    def test_a_scan_is_refused_rather_than_ocrd(self) -> None:
        # A scanned resume is not ATS-safe either, so accepting it would produce a
        # resume the systems it is sent to cannot read.
        with pytest.raises(ParseRefused, match="no_text_layer") as caught:
            extract_pdf(image_only_pdf())
        assert "no text layer" in caught.value.advice
        assert "paste the text" in caught.value.advice

    def test_a_locked_pdf_is_refused_with_the_reason(self) -> None:
        with pytest.raises(ParseRefused, match="locked") as caught:
            extract_pdf(encrypted_pdf())
        assert "password" in caught.value.advice

    def test_the_page_cap_is_enforced(self) -> None:
        with pytest.raises(ParseRefused, match="too_many_pages") as caught:
            extract_pdf(multi_page_pdf(60), ParseLimits(max_pages=50))
        assert "60 pages" in caught.value.advice

    def test_a_page_count_inside_the_cap_is_fine(self) -> None:
        assert extract_pdf(multi_page_pdf(3)).page_count == 3

    def test_the_character_cap_is_enforced(self) -> None:
        with pytest.raises(ParseRefused, match="too_much_text"):
            extract_pdf(make_pdf("x" * 5000), ParseLimits(max_characters=1000))

    def test_a_corrupt_pdf_is_refused_with_advice(self) -> None:
        with pytest.raises(ParseRefused) as caught:
            extract_pdf(b"%PDF-1.4\nthis is not really a pdf\n")
        assert caught.value.advice

    def test_an_empty_pdf_is_refused(self) -> None:
        with pytest.raises(ParseRefused):
            extract_pdf(b"%PDF-1.4\n%%EOF\n")

    def test_refusal_advice_is_written_for_a_person(self) -> None:
        # "parse error" is not something a candidate can act on.
        with pytest.raises(ParseRefused) as caught:
            extract_pdf(image_only_pdf())
        advice = caught.value.advice
        assert advice[0].isupper() or advice.startswith("this")
        assert len(advice) > 40


# ------------------------------------------------------------------------ DOCX


class TestDocx:
    def test_a_real_docx_yields_its_paragraphs_in_order(self) -> None:
        result = extract_docx(make_docx())
        lines = [line for line in result.text.splitlines() if line.strip()]
        assert lines[0] == "Jordan A. Rivera"
        assert "Built 20+ React components used by 4 teams" in lines

    def test_tabs_and_breaks_survive(self) -> None:
        # A tab is often what separates a job title from its dates, so losing it
        # loses the structure the importer reads.
        body = (
            '<?xml version="1.0"?><w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Engineer</w:t><w:tab/><w:t>Jun 2024</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
        result = extract_docx(make_docx(document_xml=body))
        assert result.text == "Engineer\tJun 2024"

    def test_table_cells_are_read(self) -> None:
        # Many resume templates lay out dates in a table.
        body = (
            '<?xml version="1.0"?><w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Accenture</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>Jun 2024</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:body></w:document>"
        )
        result = extract_docx(make_docx(document_xml=body))
        assert "Accenture" in result.text
        assert "Jun 2024" in result.text

    def test_an_xxe_payload_is_refused_not_resolved(self) -> None:
        # The reason defusedxml is a dependency: the stdlib parser resolves entities,
        # and this one points at a local file.
        body = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE doc [<!ENTITY secret SYSTEM "file:///c:/windows/win.ini">]>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>&secret;</w:t></w:r></w:p></w:body></w:document>"
        )
        with pytest.raises(ParseRefused, match="unsafe_or_malformed_xml"):
            extract_docx(make_docx(document_xml=body))

    def test_a_billion_laughs_payload_is_refused(self) -> None:
        body = (
            '<?xml version="1.0"?>'
            "<!DOCTYPE doc ["
            '<!ENTITY a "aaaaaaaaaa">'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
            '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
            "]>"
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>&c;</w:t></w:r></w:p></w:body></w:document>"
        )
        with pytest.raises(ParseRefused, match="unsafe_or_malformed_xml"):
            extract_docx(make_docx(document_xml=body))

    def test_a_zip_bomb_is_refused_by_its_declared_size(self) -> None:
        # Highly compressible filler: small on disk, enormous expanded.
        bomb = make_docx(extra_parts={"word/media/pad.bin": "0" * (5 * 1024 * 1024)})
        with pytest.raises(ParseRefused, match=r"archive_too_large|suspicious_compression"):
            extract_docx(bomb, ParseLimits(max_uncompressed_bytes=1024 * 1024))

    def test_a_suspicious_compression_ratio_is_refused(self) -> None:
        bomb = make_docx(extra_parts={"word/media/pad.bin": "0" * (2 * 1024 * 1024)})
        with pytest.raises(ParseRefused, match="suspicious_compression"):
            extract_docx(bomb, ParseLimits(max_compression_ratio=20))

    def test_too_many_parts_is_refused(self) -> None:
        crowded = make_docx(extra_parts={f"word/media/{index}.bin": "x" for index in range(600)})
        with pytest.raises(ParseRefused, match="too_many_parts"):
            extract_docx(crowded, ParseLimits(max_archive_entries=500))

    def test_an_external_relationship_is_reported_and_never_followed(self) -> None:
        # A document can point at a remote template or image. ARIA parses the file; it
        # does not fetch what the file points at.
        rels = (
            '<?xml version="1.0"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/image" Target="https://evil.test/track.png" '
            'TargetMode="External"/></Relationships>'
        )
        result = extract_docx(make_docx(extra_parts={"word/_rels/document.xml.rels": rels}))
        assert "external relationship" in result.detail
        assert "evil.test" not in result.detail
        assert "evil.test" not in result.text

    def test_header_text_is_reported_and_left_out_of_the_body(self) -> None:
        # Contact details in a header are often invisible to an ATS
        # (RESUME_ENGINE §1.8), so their presence is worth saying out loud.
        header = docx_document_xml(["jordan.rivera@example.com | +1 555 0100"]) + " " * 300
        result = extract_docx(make_docx(extra_parts={"word/header1.xml": header}))
        assert result.has_header_or_footer_text is True
        assert "header or footer" in result.detail
        assert "555 0100" not in result.text

    def test_an_archive_without_a_body_is_refused(self) -> None:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
        with pytest.raises(ParseRefused, match="not_a_word_document"):
            extract_docx(buffer.getvalue())

    def test_a_corrupt_archive_is_refused(self) -> None:
        with pytest.raises(ParseRefused, match="unreadable_docx"):
            extract_docx(b"PK\x03\x04 and then nonsense")

    def test_an_empty_body_is_a_failure_not_a_silent_success(self) -> None:
        with pytest.raises(ParseFailed, match="no text"):
            extract_docx(make_docx(paragraphs=[]))

    def test_python_docx_is_not_a_dependency(self) -> None:
        # Reading word/document.xml ourselves removes lxml and the question of how its
        # entity resolution is configured.
        from pathlib import Path

        manifest = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        assert "python-docx" not in manifest
        assert "defusedxml" in manifest


# ------------------------------------------------------------------- plain text


class TestPlainText:
    def test_text_is_returned_with_line_endings_normalised(self) -> None:
        # Line offsets are recorded on every draft fact, so CRLF and LF must count the
        # same or a fact would point at the wrong line.
        result = extract_text_file(b"React\r\nPostgreSQL\rDocker\n")
        assert result.text == "React\nPostgreSQL\nDocker\n"

    def test_the_v1_skills_notes_shape_parses(self) -> None:
        notes = b"Frontend: React, Next.js, TypeScript\nBackend: FastAPI, PostgreSQL\n"
        result = extract_text_file(notes)
        assert "Next.js" in result.text
        assert "2 line(s)" in result.detail or "3 line(s)" in result.detail

    def test_non_utf8_is_refused_with_advice(self) -> None:
        with pytest.raises(ParseRefused, match="not_utf8") as caught:
            extract_text_file(b"\xff\xfeResume in UTF-16")
        assert "UTF-8" in caught.value.advice

    def test_the_character_cap_is_enforced(self) -> None:
        with pytest.raises(ParseRefused, match="too_much_text"):
            extract_text_file(b"x" * 2000, ParseLimits(max_characters=1000))


# ----------------------------------------------------------------------- images


class TestImages:
    @pytest.mark.parametrize("image_type", ["png", "jpeg"])
    def test_an_image_is_refused_because_there_is_no_ocr(self, image_type: str) -> None:
        with pytest.raises(ParseRefused, match="image_upload") as caught:
            parse_bytes(b"\x89PNG\r\n\x1a\n", image_type)
        assert "does not read text from images" in caught.value.advice

    def test_an_unknown_type_is_a_failure_not_a_refusal(self) -> None:
        # A refusal is a message to the candidate; this is a message to us.
        with pytest.raises(ParseFailed, match="no parser for type"):
            parse_bytes(b"data", "wingdings")


# ------------------------------------------------------------------ the worker


class TestTheIsolatedWorker:
    async def test_a_pdf_parses_through_a_subprocess(self) -> None:
        result = await SubprocessDocumentParser().parse(make_pdf(), "pdf")
        assert "Resume text" in result.text
        assert result.extractor == "pdfplumber"
        assert "extractor=pdfplumber" in result.detail

    async def test_the_layout_crosses_the_boundary_intact(self) -> None:
        content = (FIXTURES / "clean_inline_block.pdf").read_bytes()
        crossed = await SubprocessDocumentParser().parse(content, "pdf")
        local = await InProcessDocumentParser().parse(content, "pdf")
        assert crossed.layout is not None
        assert crossed.layout == local.layout

    def test_a_layout_that_does_not_validate_is_a_failure_not_an_empty_value(self) -> None:
        # What a subverted worker would send. The importer reads the document from the
        # layout, so a malformed one stops here.
        with pytest.raises(ParseFailed, match="does not validate"):
            client._layout('{"triage": "readable", "lines": [{"index": 3}]}')
        with pytest.raises(ParseFailed, match="not JSON text"):
            client._layout({"triage": "readable"})

    async def test_a_docx_parses_through_a_subprocess(self) -> None:
        result = await SubprocessDocumentParser().parse(make_docx(), "docx")
        assert "Jordan A. Rivera" in result.text

    async def test_a_refusal_crosses_the_boundary_as_a_refusal(self) -> None:
        # A crashed worker and a refused file must be impossible to confuse.
        with pytest.raises(ParseRefused, match="no_text_layer"):
            await SubprocessDocumentParser().parse(image_only_pdf(), "pdf")

    async def test_a_timeout_kills_the_worker_and_reports_it(self) -> None:
        # A PDF can make an extractor loop; the timeout is the only reliable answer.
        parser = SubprocessDocumentParser(ParseLimits(timeout_seconds=0.05))
        with pytest.raises(ParseFailed, match="did not finish within"):
            await parser.parse(make_pdf(), "pdf")

    async def test_library_warnings_do_not_corrupt_the_result(self) -> None:
        # pdfminer writes to stderr about real-world PDFs; stdout carries only JSON.
        result = await SubprocessDocumentParser().parse(make_pdf(), "pdf")
        assert result.text.strip()

    async def test_each_parse_gets_its_own_process(self) -> None:
        # Two files in flight must not share state, because one of them may be built
        # to corrupt it.
        first, second = await asyncio.gather(
            SubprocessDocumentParser().parse(make_pdf("First document. " * 20), "pdf"),
            SubprocessDocumentParser().parse(make_pdf("Second document. " * 20), "pdf"),
        )
        assert "First document" in first.text
        assert "Second document" in second.text
        assert "Second document" not in first.text

    async def test_the_in_process_parser_agrees_with_the_subprocess_one(self) -> None:
        # The fast path used by other tests must not drift from the real one.
        content = make_docx()
        subprocess_result = await SubprocessDocumentParser().parse(content, "docx")
        in_process_result = await InProcessDocumentParser().parse(content, "docx")
        assert subprocess_result.text == in_process_result.text


class TestTheWorkerHasNoNetwork:
    def test_no_module_in_the_parser_imports_a_network_library(self) -> None:
        """SECURITY.md §7 wants the parse sandboxed. This is the checkable part.

        The narrow promise, checkable from source: nothing the parser imports can open
        a socket. Native code is not bound by imports, which is why the parse also runs
        in a container with no network; test_isolation.py checks that one.
        """
        import ast
        from pathlib import Path

        # Socket-capable modules only. urllib.parse is string manipulation — it is
        # what splits a URI to decide whether to keep it — and forbidding it would be
        # banning a name rather than a capability.
        forbidden = {
            "socket",
            "ssl",
            "http",
            "http.client",
            "urllib.request",
            "urllib.error",
            "httpx",
            "requests",
            "aiohttp",
            "ftplib",
            "smtplib",
            "xmlrpc",
            "webbrowser",
        }
        allowed_despite_prefix = {"urllib.parse"}

        package = Path(__file__).resolve().parents[1] / "aria_document_parser"
        violations: list[str] = []
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                line = getattr(node, "lineno", 0)
                for name in names:
                    if name in allowed_despite_prefix:
                        continue
                    root = name.split(".")[0]
                    if name in forbidden or root in forbidden:
                        violations.append(f"{path.name}:{line} imports {name}")

        assert not violations, (
            f"the parse worker handles hostile files and must not be able to reach the network: {violations}"
        )

    def test_the_client_is_allowed_to_use_asyncio_subprocess(self) -> None:
        # The client spawns the worker, so it imports asyncio; the check above targets
        # socket-capable modules rather than asyncio as a whole.
        from aria_document_parser import client

        assert hasattr(client, "SubprocessDocumentParser")


class TestLimitsAreDocumented:
    def test_every_limit_has_a_comment_explaining_what_it_defends(self) -> None:
        # A limit whose reason is forgotten gets raised the first time someone hits it.
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "aria_document_parser" / "limits.py").read_text(
            encoding="utf-8"
        )
        fields = [
            line.split(":")[0].strip()
            for line in source.splitlines()
            if line.startswith("    ") and ": " in line and "=" in line and not line.strip().startswith("#")
        ]
        assert len(fields) >= 8
        # Each field is preceded by a #: comment block.
        assert source.count("#:") >= len(fields)


class TestLinkAnnotations:
    """A resume's LinkedIn and GitHub URLs live in annotations, not in the text."""

    def test_a_link_annotation_is_extracted_as_data(self) -> None:
        result = extract_pdf(
            make_pdf_with_links(["https://github.com/example", "https://linkedin.com/in/example"])
        )
        assert "https://github.com/example" in result.links
        assert "https://linkedin.com/in/example" in result.links

    def test_links_are_appended_under_a_heading_so_they_are_not_bullets(self) -> None:
        # The importer has to be able to tell a URL from a resume line.
        result = extract_pdf(make_pdf_with_links(["https://github.com/example"]))
        assert LINKS_HEADING in result.text
        heading_at = result.text.index(LINKS_HEADING)
        assert "https://github.com/example" in result.text[heading_at:]

    def test_a_mailto_link_is_kept_because_it_carries_an_email(self) -> None:
        result = extract_pdf(make_pdf_with_links(["mailto:jordan.rivera@example.com"]))
        assert "mailto:jordan.rivera@example.com" in result.links

    @pytest.mark.parametrize(
        "hostile",
        [
            "javascript:fetch('https://evil.test')",
            "file:///c:/windows/win.ini",
            "data:text/html;base64,PHNjcmlwdD4=",
            "ftp://evil.test/payload",
        ],
    )
    def test_a_link_with_a_dangerous_scheme_is_dropped(self, hostile: str) -> None:
        result = extract_pdf(make_pdf_with_links([hostile]))
        assert result.links == ()
        assert LINKS_HEADING not in result.text

    def test_duplicate_links_are_collapsed(self) -> None:
        # A header link repeats on every page.
        repeated = ["https://github.com/example"] * 5
        assert extract_pdf(make_pdf_with_links(repeated)).links == ("https://github.com/example",)

    def test_the_number_of_links_is_capped(self) -> None:
        many = [f"https://example.test/{index}" for index in range(200)]
        result = extract_pdf(make_pdf_with_links(many), ParseLimits(max_links_total=10))
        assert len(result.links) == 10

    def test_an_over_long_uri_is_truncated_rather_than_stored_whole(self) -> None:
        long_uri = "https://example.test/" + "a" * 2000
        result = extract_pdf(make_pdf_with_links([long_uri]), ParseLimits(max_link_length=100))
        assert all(len(link) <= 100 for link in result.links)

    def test_nothing_is_fetched(self) -> None:
        """The parser must not become a request-forgery tool for whoever sent the file.

        Enforced structurally by the no-network import check above; asserted here on
        the behaviour, so a future change that adds a fetch fails a test about intent
        rather than only a test about imports.
        """
        result = extract_pdf(make_pdf_with_links(["https://evil.test/collect"]))
        # The URI is recorded as text and nothing more.
        assert result.links == ("https://evil.test/collect",)


class TestTheCrossCheck:
    """§5.6: whether pypdf reads the same lines in the same order. A diagnostic, never a score."""

    def test_a_clean_resume_agrees_and_needs_no_review(self) -> None:
        result = extract_pdf((FIXTURES / "clean_inline_block.pdf").read_bytes())
        assert result.layout.cross_extractor is CrossExtractor.AGREE
        assert result.needs_review is False

    def test_a_float_scrambled_resume_disagrees_and_is_flagged(self) -> None:
        # pypdf reads eight entry headers ahead of the body; the geometry does not.
        result = extract_pdf((FIXTURES / "float_scrambled.pdf").read_bytes())
        assert result.layout.cross_extractor is CrossExtractor.DISAGREE
        assert result.needs_review is True
        assert "different order" in result.detail
        assert "%" not in result.detail

    def test_flagged_is_not_withheld_and_the_geometry_s_order_stands(self) -> None:
        clean = extract_pdf((FIXTURES / "clean_inline_block.pdf").read_bytes())
        scrambled = extract_pdf((FIXTURES / "float_scrambled.pdf").read_bytes())
        assert scrambled.text == clean.text

    def test_an_interleaved_two_column_page_is_rebuilt_and_flagged(self) -> None:
        left = [
            "EXPERIENCE",
            "Nordvale Consulting - Software Engineer",
            "Built 20+ React components used by 4 teams",
        ]
        right = ["Python, TypeScript, C++", "AWS, Docker, Kubernetes", "PostgreSQL, Redis, Kafka"]
        result = extract_pdf(two_column_pdf(left * 3, right * 3))
        assert result.column_pages == (1,)
        assert result.layout.cross_extractor is CrossExtractor.DISAGREE
        assert result.needs_review is True
        lines = result.text.splitlines()
        assert lines.index("Built 20+ React components used by 4 teams") < lines.index(
            "Python, TypeScript, C++"
        )

    def test_a_page_whose_column_order_does_not_follow_is_refused(self) -> None:
        left = ["Achievement line number one for the left column"] * 9
        right = ["Skills sidebar entry"] * 9
        banner = "PROFESSIONAL DEVELOPMENT, AWARDS, CERTIFICATIONS AND SELECTED PUBLICATIONS 2024"
        with pytest.raises(ParseRefused, match="ambiguous_columns") as caught:
            extract_pdf(two_column_pdf(left, right, banner=(banner, 640)))
        assert "wrong dates" in caught.value.advice
