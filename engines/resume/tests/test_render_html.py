"""Stage 7, the HTML half: the page the printer receives, and the template's text-layer rules."""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from importlib import resources

import pytest
from aria_resume.document import (
    ContactItem,
    DocumentBullet,
    DocumentEntry,
    DocumentSection,
    ResumeDocument,
    SectionKind,
)
from aria_resume.render.html import render_html, stylesheet, template_source
from aria_resume.render.lint import lint, lint_stylesheet, lint_template


def _document(*, bullet: str = "Cut p95 API latency by 30% with Redis caching") -> ResumeDocument:
    return ResumeDocument(
        name="Jordan A. Rivera",
        contact=(
            ContactItem(text="jordan.rivera@example.com", url="mailto:jordan.rivera@example.com"),
            ContactItem(text="(555) 014-2288"),
            ContactItem(
                text="linkedin.com/in/jordan-rivera-example",
                url="https://linkedin.com/in/jordan-rivera-example",
            ),
        ),
        sections=(
            DocumentSection(
                kind=SectionKind.SUMMARY, lines=("Software engineer with three years of backend work.",)
            ),
            DocumentSection(
                kind=SectionKind.EXPERIENCE,
                entries=(
                    DocumentEntry(
                        title="Nordvale Consulting",
                        detail="Software Engineer",
                        dates="Feb 2024 \u2013 Jul 2025",
                        subline="Pune, India",
                        bullets=(DocumentBullet(text=bullet), DocumentBullet(text="Built 15+ React screens")),
                    ),
                ),
            ),
        ),
    )


class _Elements(HTMLParser):
    """Each div's class, its direct child tags, and its text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict[str, object]] = []
        self.divs: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.stack:
            children = self.stack[-1]["children"]
            assert isinstance(children, list)
            children.append(tag)
        element: dict[str, object] = {
            "tag": tag,
            "class": dict(attrs).get("class"),
            "children": [],
            "text": "",
        }
        self.stack.append(element)
        if tag == "div":
            self.divs.append(element)

    def handle_endtag(self, tag: str) -> None:
        while self.stack and self.stack.pop()["tag"] != tag:
            pass

    def handle_data(self, data: str) -> None:
        for element in self.stack:
            element["text"] = str(element["text"]) + data


def _divs(html: str, cls: str) -> list[dict[str, object]]:
    parser = _Elements()
    parser.feed(html[html.index("<body>") :])
    return [div for div in parser.divs if div["class"] == cls]


class TestThePage:
    def test_the_contact_line_is_one_unstyled_text_node(self) -> None:
        (contact,) = _divs(render_html(_document()), "contact")
        assert contact["children"] == []
        assert contact["text"] == (
            "jordan.rivera@example.com | (555) 014-2288 | linkedin.com/in/jordan-rivera-example"
        )

    def test_every_bullet_begins_with_a_literal_bullet_character(self) -> None:
        bullets = _divs(render_html(_document()), "bullet")
        assert [str(bullet["text"]) for bullet in bullets] == [
            "\u2022\u00a0Cut p95 API latency by 30% with Redis caching",
            "\u2022\u00a0Built 15+ React screens",
        ]

    def test_an_entry_header_holds_its_title_and_its_dates_in_the_band(self) -> None:
        html = render_html(_document())
        assert (
            '<span class="what"><b>Nordvale Consulting</b> &mdash; Software Engineer</span>'
            '<span class="when">Feb 2024 \u2013 Jul 2025</span>'
        ) in html

    def test_the_headings_are_the_standard_ones_in_capitals(self) -> None:
        assert [str(div["text"]) for div in _divs(render_html(_document()), "heading")] == [
            "SUMMARY",
            "EXPERIENCE",
        ]

    def test_text_is_escaped_never_markup(self) -> None:
        html = render_html(_document(bullet='Shipped <script>alert("x")</script> & a <b>bold</b> claim'))
        assert "<script>" not in html and "<b>bold</b>" not in html
        assert "&lt;script&gt;" in html

    def test_the_same_document_renders_to_the_same_bytes(self) -> None:
        assert render_html(_document()) == render_html(_document())

    def test_the_page_needs_nothing_from_the_network(self) -> None:
        html = render_html(_document())
        assert not re.search(r"(?:src|href)\s*=|url\((?!data:)", html)
        assert html.count("@font-face") == 3


class TestTheShippedTemplatePassesItsOwnRules:
    def test_no_findings(self) -> None:
        assert lint(stylesheet(), template_source()) == []


class TestTheLintCatchesEachRule:
    @pytest.mark.parametrize(
        ("css", "finding"),
        [
            (".when { float: right; }", "float"),
            (".bullet { list-style: disc; }", "list-style"),
            (".x { position: absolute; }", "absolute"),
            ("@page { @top-center { content: 'x'; } }", "margin box"),
            ('.x { background: url("https://example.com/a.png"); }', "external url"),
        ],
    )
    def test_a_stylesheet_rule(self, css: str, finding: str) -> None:
        good = stylesheet()
        assert any(finding in problem for problem in lint_stylesheet(good + "\n" + css))

    def test_ligatures_left_on(self) -> None:
        css = stylesheet().replace('font-feature-settings: "liga" 0, "clig" 0, "dlig" 0;', "")
        assert any("ligatures" in problem for problem in lint_stylesheet(css))

    def test_a_date_band_starting_left_of_55_percent(self) -> None:
        css = stylesheet().replace("width: 72%;", "width: 50%;")
        assert any("55%" in problem for problem in lint_stylesheet(css))

    @pytest.mark.parametrize(
        ("change", "finding"),
        [
            (
                (
                    '<div class="contact">{{ document.contact_line }}</div>',
                    '<div class="contact"><a href="x">{{ document.contact_line }}</a></div>',
                ),
                "contact line",
            ),
            (('<div class="bullet">', '<ul><li class="bullet">'), "<ul>"),
            (("{{ bullet.text }}", "{{ bullet.text | safe }}"), "escaping"),
            (("<body>", "<body><table>"), "<table>"),
            (("<body>", '<body><img src="x">'), "<img>"),
        ],
    )
    def test_a_template_rule(self, change: tuple[str, str], finding: str) -> None:
        template = template_source()
        assert change[0] in template
        assert any(finding in problem for problem in lint_template(template.replace(change[0], change[1])))


class TestTheFonts:
    @pytest.mark.parametrize(
        ("name", "digest"),
        [
            ("Carlito-Regular.ttf", "f6418f708baede9789daef5d458c0f53d2a888af9820e8062934e504fedc6595"),
            ("Carlito-Bold.ttf", "bb5d20f79b82599ec72983597437373a80f2d2085fa91fc144fd74e876a594db"),
            ("Carlito-Italic.ttf", "0b019225e58d702bfedcbd35c21696769f8ee115cb6343f84c2f240312450d1c"),
            ("OFL.txt", "58402f82a7c332a700294988fe7554fbb0a63a8d27ccc1ee3bbc640311990a00"),
        ],
    )
    def test_each_file_is_the_one_pinned_in_source_md(self, name: str, digest: str) -> None:
        data = (resources.files("aria_resume.render") / "fonts" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
